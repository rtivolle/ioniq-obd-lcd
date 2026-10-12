"""Fixed-profile vehicle operations; all adapter I/O belongs to the OBD worker."""

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import re
import secrets
import time

from diagnostics import can_payloads

CATALOG = json.loads((Path(__file__).parent / 'docs/vehicle/ioniq-2017.json').read_text())
SETTING_DEFINITIONS = CATALOG['persistent_settings']
# Candidate configuration pages only; none identifies a writable setting yet.
SETTING_CAPTURE_READS = {
    'ac_charge_limit_pct': [('VCU', '21F2')],
    'dc_charge_limit_pct': [('VCU', '21F2')],
    'auto_lock': [('BCM', '22B001'), ('BCM', '22B009')],
    'auto_unlock': [('BCM', '22B001'), ('BCM', '22B009')],
}


class VehicleManagement:
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.pending = {}
        with diagnostics.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS vehicle_setting_requests (
                profile TEXT NOT NULL, setting TEXT NOT NULL, desired_json TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(profile, setting))''')

    def catalog(self):
        result = copy.deepcopy(CATALOG)
        for ecu in result['ecus'].values():
            for control in ecu['controls'].values():
                control.pop('activate', None)
                control.pop('release', None)
        result['validation'] = 'Community definitions; not validated on this vehicle or adapter.'
        return result

    def settings(self):
        with self.diagnostics.db() as conn:
            desired = {row[0]: {'desired_value': json.loads(row[1]), 'updated_at': row[2]}
                       for row in conn.execute('SELECT setting, desired_json, updated_at FROM vehicle_setting_requests WHERE profile=?', (CATALOG['profile'],))}
        return {'profile': CATALOG['profile'], 'settings': {
            key: {**definition, **desired.get(key, {'desired_value': None, 'updated_at': None}),
                  'vehicle_value': None, 'read_supported': False, 'write_supported': False,
                  'status': 'pending_protocol' if key in desired else 'unsupported',
                  'reason': 'No verified persistent read/write protocol for this vehicle. Desired values are stored locally only; not applied to the vehicle.'}
            for key, definition in SETTING_DEFINITIONS.items()}}

    def save_desired(self, values):
        if not isinstance(values, dict) or not values:
            raise ValueError('Provide at least one desired setting')
        for key, value in values.items():
            definition = SETTING_DEFINITIONS.get(key)
            if definition is None:
                raise ValueError('Unknown persistent setting')
            if definition['type'] == 'integer':
                if type(value) is not int or not definition['min'] <= value <= definition['max']:
                    raise ValueError(f'{key} requires an integer from {definition["min"]} to {definition["max"]}')
            elif value not in definition['choices']:
                raise ValueError(f'Unsupported preference for {key}')
        with self.diagnostics.db() as conn:
            for key, value in values.items():
                conn.execute('''INSERT INTO vehicle_setting_requests VALUES (?, ?, ?, ?)
                    ON CONFLICT(profile, setting) DO UPDATE SET desired_json=excluded.desired_json, updated_at=excluded.updated_at''',
                    (CATALOG['profile'], key, json.dumps(value), time.time()))
        return {'success': True, 'message': 'Desired settings saved locally. Not applied to the vehicle.', **self.settings()}

    @staticmethod
    def apply_setting(key):
        if key not in SETTING_DEFINITIONS:
            raise ValueError('Unknown persistent setting')
        raise ValueError('Persistent vehicle writes unavailable: verified read/write protocol and readback are required')

    def capture_setting(self, obd, payload):
        """Collect before/after evidence for manual changes without guessing writes."""
        setting = payload.get('setting')
        if setting not in SETTING_CAPTURE_READS:
            raise ValueError('Unknown persistent setting')
        before_id = payload.get('before_report_id')
        before = None
        if before_id is not None:
            if type(before_id) is not int:
                raise ValueError('Invalid baseline report')
            before = self.diagnostics.report(before_id)
            if not before or before.get('operation') != 'setting_capture' or before.get('phase') != 'before' or before.get('setting') != setting or before.get('adapter_mac') != obd.conn.mac:
                raise ValueError('Baseline must match this setting and adapter')
        report = {'operation': 'setting_capture', 'setting': setting,
                  'created_at': time.time(), 'adapter_mac': obd.conn.mac,
                  'profile': CATALOG['profile'], 'phase': 'after' if before else 'before',
                  'before_report_id': before_id, 'reads': {}, 'changed_payload_bytes': {},
                  'message': 'Candidate pages only. Differences can be live telemetry; they do not establish a setting location or authorize a write.'}
        for ecu, pid in SETTING_CAPTURE_READS[setting]:
            key = f'{ecu}:{pid}'
            reading = self.read(obd, ecu, pid)
            report['reads'][key] = reading
            old = before['reads'].get(key, {}) if before else {}
            if reading['status'] == old.get('status') == 'responding':
                previous = bytes.fromhex(old['payload_hex'])
                current = bytes.fromhex(reading['payload_hex'])
                report['changed_payload_bytes'][key] = [
                    {'isotp_index': index, 'before': previous[index] if index < len(previous) else None,
                     'after': current[index] if index < len(current) else None}
                    for index in range(max(len(previous), len(current)))
                    if (previous[index] if index < len(previous) else None) != (current[index] if index < len(current) else None)]
        report['id'] = self.diagnostics.save(report)
        return report

    @staticmethod
    def ecu(name):
        if name not in CATALOG['ecus']:
            raise ValueError('Unknown ECU')
        return CATALOG['ecus'][name]

    def control(self, ecu_name, did):
        control = self.ecu(ecu_name)['controls'].get(did)
        if not control or not control['available']:
            raise ValueError('Control unavailable: documented command and operating prerequisites required')
        if not all(re.fullmatch(r'2F[0-9A-F]{4}(?:03|00)(?:[0-9A-F]{2})*', control[k] or '') for k in ('activate', 'release')):
            raise ValueError('Invalid control definition')
        return control

    @contextmanager
    def target(self, obd, ecu):
        # Physical addressing needs flow-control frames sent to this ECU too.
        with self.diagnostics.headers(obd):
            try:
                obd.set_header(ecu['tx_id'])
                for command in (f'AT FC SH {ecu["tx_id"]}', 'AT FC SD 30 00 00', 'AT FC SM 1'):
                    if 'OK' not in obd._send_command(command, timeout=.5).upper():
                        raise ConnectionError('Adapter does not support the required flow control')
                yield
            finally:
                if 'OK' not in obd._send_command('AT FC SM 0', timeout=.5).upper():
                    obd.conn.close()
                    raise ConnectionError('Could not restore automatic flow control')

    @staticmethod
    def response(obd, ecu, command, prefix, raw_responses=None):
        raw = obd._send_command(command, timeout=2)
        if raw_responses is not None:
            raw_responses[command] = raw
        payload = can_payloads(raw).get(ecu['rx_id'])
        if not payload or not payload.startswith(prefix):
            raise ConnectionError('ECU response missing, rejected, incomplete, or unexpected')
        return raw, payload

    def read(self, obd, ecu_name, pid):
        ecu = self.ecu(ecu_name)
        if pid not in ecu['pids']:
            raise ValueError('Read request not in the vehicle allowlist')
        command = bytes.fromhex(pid)
        prefix = bytes([command[0] + 0x40]) + command[1:]
        report = {'operation': 'vehicle_read', 'created_at': time.time(), 'ecu': ecu_name,
                  'pid': pid, 'validation': 'Community definition; not validated on this vehicle',
                  'parameters': ecu['pids'][pid]['parameters'], 'status': 'unavailable'}
        with self.target(obd, ecu):
            try:
                if ecu['session']:
                    self.response(obd, ecu, '10 03', b'\x50\x03')
                raw = obd._send_command(pid, timeout=2)
                payload = can_payloads(raw).get(ecu['rx_id'])
                report['raw_response'] = raw
                if payload and payload.startswith(prefix):
                    report.update(status='responding', payload_hex=payload.hex().upper())
            finally:
                if ecu['session']:
                    try:
                        self.response(obd, ecu, '10 01', b'\x50\x01')
                    except Exception:
                        obd.conn.close()
                        raise
        report['id'] = self.diagnostics.save(report)
        return report

    def prepare(self, obd, payload):
        ecu_name, did = payload.get('ecu'), payload.get('control')
        control = self.control(ecu_name, did)
        if payload.get('profile_confirmed') is not True or payload.get('conditions_confirmed') is not True:
            raise ValueError('Confirm the vehicle profile and parked/READY-off conditions')
        state = payload.get('vehicle_state')
        if state not in control['vehicle_states'] or state in ('READY', 'CHARGING'):
            raise ValueError('Selected vehicle state is not supported for this operation')
        self.diagnostics.check_parked(obd)
        now = time.time()
        self.pending = {key: value for key, value in self.pending.items() if value['expires'] > now}
        token = secrets.token_urlsafe(24)
        self.pending[token] = {'ecu': ecu_name, 'did': did, 'state': state, 'connection': obd.conn, 'expires': now + 60}
        return {'success': True, 'confirmation_token': token, 'expires_at': now + 60,
                'label': control['label'], 'message': 'Confirm a temporary actuator command; not a persistent setting.'}

    def execute(self, obd, payload):
        if payload.get('confirm') is not True or payload.get('conditions_confirmed') is not True:
            raise ValueError('Explicit operation confirmation required')
        pending = self.pending.pop(str(payload.get('confirmation_token', '')), None)
        if not pending or pending['expires'] <= time.time() or pending['connection'] is not obd.conn:
            raise ValueError('Confirmation expired, used, or adapter reconnected')
        self.diagnostics.check_parked(obd)
        ecu = self.ecu(pending['ecu'])
        control = self.control(pending['ecu'], pending['did'])
        audit = {'operation': 'vehicle_control', 'created_at': time.time(), 'ecu': pending['ecu'],
                 'control': pending['did'], 'label': control['label'], 'adapter_mac': obd.conn.mac,
                 'vehicle_state_confirmed': pending['state'], 'status': 'sending', 'raw_responses': {}}
        audit_id = self.diagnostics.save(audit)  # Record intent before any state change.
        try:
            with self.target(obd, ecu):
                attempted = False
                try:
                    raw, _ = self.response(obd, ecu, '10 03', b'\x50\x03', audit['raw_responses'])
                    attempted = True
                    activate = control['activate']
                    raw, _ = self.response(obd, ecu, activate, bytes.fromhex('6F' + activate[2:8]), audit['raw_responses'])
                    time.sleep(.5)
                    audit['status'] = 'acknowledged'
                finally:
                    # Release even when activation reply is lost; never resend activation.
                    recovery_errors = []
                    try:
                        if attempted:
                            release = control['release']
                            raw, _ = self.response(obd, ecu, release, bytes.fromhex('6F' + release[2:8]), audit['raw_responses'])
                    except Exception as exc:
                        recovery_errors.append(str(exc))
                    # Still try to exit the session when release is not acknowledged.
                    try:
                        raw, _ = self.response(obd, ecu, '10 01', b'\x50\x01', audit['raw_responses'])
                    except Exception as exc:
                        recovery_errors.append(str(exc))
                    if recovery_errors:
                        obd.conn.close()
                        raise ConnectionError('; '.join(recovery_errors))
        except Exception as exc:
            audit.update(status='unconfirmed', error=str(exc))
        with self.diagnostics.db() as conn:
            conn.execute('UPDATE diagnostic_reports SET body=? WHERE id=?', (json.dumps(audit), audit_id))
        return {'success': True, 'audit_id': audit_id, **audit,
                'message': 'Command and release acknowledged; verify the physical result.' if audit['status'] == 'acknowledged'
                else 'Outcome unconfirmed. Inspect the vehicle before any further operation; do not automatically retry.'}
