"""Worker-only, allowlisted CAN diagnostics. No arbitrary commands or actuators."""

from contextlib import contextmanager
import json
import re
import secrets
import sqlite3
import time

ELM_SOURCE = 'https://www.elmelectronics.com/wp-content/uploads/2016/07/ELM327DS.pdf'
PID_SOURCE = 'https://github.com/JejuSoul/OBD-PIDs-for-HKMC-EVs/tree/master/Ioniq%20EV%20-%2028kWh/extendedpids'
PROFILE = '2017 Hyundai Ioniq Electric 28 kWh, Canada'
DEFINITIONS = {
    'P0133': {'description': 'Oxygen sensor circuit slow response', 'source': ELM_SOURCE, 'page': 34},
}


def can_payloads(raw):
    """Reassemble interleaved 11-bit ISO-TP responses with ATH1, CAF1, DLC off.

    Reject incomplete, duplicate or out-of-sequence messages rather than decoding
    truncated data as a clean diagnostic result.
    """
    completed, pending = {}, {}
    for line in raw.upper().splitlines():
        compact = re.sub(r'\s+', '', line)
        if not re.fullmatch(r'[0-9A-F]{3}(?:[0-9A-F]{2})+', compact):
            continue
        ecu, frame = compact[:3], bytes.fromhex(compact[3:])
        kind = frame[0] >> 4
        if kind == 0:
            length = frame[0] & 15
            if 0 < length <= len(frame) - 1 and ecu not in completed:
                completed[ecu] = frame[1:1 + length]
        elif kind == 1 and len(frame) >= 2:
            length = ((frame[0] & 15) << 8) | frame[1]
            if 7 < length <= 4095:
                pending[ecu] = (length, bytearray(frame[2:]), 1)
        elif kind == 2 and ecu in pending:
            length, payload, sequence = pending.pop(ecu)
            if frame[0] & 15 != sequence:
                continue
            payload.extend(frame[1:])
            if len(payload) >= length:
                completed[ecu] = bytes(payload[:length])
            else:
                pending[ecu] = (length, payload, (sequence + 1) % 16)
    return completed


def decode_dtcs(payload, service):
    if not payload or payload[0] != service + 0x40 or len(payload) < 2:
        return None
    count = payload[1]  # ISO 15765-4 includes a DTC count after the service byte.
    if len(payload) < 2 + count * 2:
        return None
    codes = []
    for index in range(count):
        high, low = payload[2 + index * 2:4 + index * 2]
        if high == low == 0:
            return None
        code = f"{'PCBU'[high >> 6]}{(high >> 4) & 3}{high & 15:X}{low:02X}"
        codes.append({'code': code, **DEFINITIONS.get(code, {
            'description': 'Verified definition unavailable; consult Hyundai service information',
            'source': None})})
    return codes


class Diagnostics:
    def __init__(self, db_path):
        self.db_path = db_path
        self.pending_clear = {}
        with self.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS diagnostic_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, body TEXT NOT NULL)''')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, report):
        with self.db() as conn:
            return conn.execute('INSERT INTO diagnostic_reports (created_at, body) VALUES (?, ?)',
                                (time.time(), json.dumps(report))).lastrowid

    def reports(self):
        with self.db() as conn:
            return [{'id': row[0], **json.loads(row[1])} for row in
                    conn.execute('SELECT id, body FROM diagnostic_reports ORDER BY id DESC LIMIT 50')]

    def report(self, report_id):
        with self.db() as conn:
            row = conn.execute('SELECT body FROM diagnostic_reports WHERE id=?', (report_id,)).fetchone()
        return {'id': report_id, **json.loads(row[0])} if row else None

    @contextmanager
    def headers(self, obd):
        if not obd.conn or not obd.conn.connected:
            raise ConnectionError('Adapter disconnected')
        if 'OK' not in obd._send_command('AT H1', timeout=.5).upper():
            raise ConnectionError('Adapter cannot enable response headers')
        try:
            yield
        finally:
            if 'OK' not in obd._send_command('AT H0', timeout=.5).upper():
                # Do not allow subsequent polling to use the wrong framing.
                obd.conn.close()
                raise ConnectionError('Could not restore adapter response formatting')
            obd._current_header = ''

    def read(self, obd):
        report = {'created_at': time.time(), 'profile': PROFILE, 'adapter_mac': obd.conn.mac,
                  'scope': 'Standard OBD responders only; Hyundai-specific ECU faults are not queried',
                  'modules': {}, 'raw_responses': {}, 'sources': [ELM_SOURCE]}
        with self.headers(obd):
            obd.set_header('7DF')
            for command, prefix in (('01 00', b'\x41\x00'), ('09 00', b'\x49\x00'),
                                    ('09 02', b'\x49\x02'), ('03', b'\x43'),
                                    ('07', b'\x47'), ('0A', b'\x4a')):
                raw = obd._send_command(command, timeout=2)
                report['raw_responses'][command] = raw
                for ecu, payload in can_payloads(raw).items():
                    if not payload.startswith(prefix):
                        continue
                    module = report['modules'].setdefault(ecu, {'ecu': ecu, 'status': 'responding',
                        'supported_pid_groups': {}, 'identification': None, 'faults': {}})
                    if command in ('01 00', '09 00') and len(payload) >= 6:
                        bitmap = int.from_bytes(payload[2:6], 'big')
                        module['supported_pid_groups'][command[:2]] = [pid for pid in range(1, 33) if bitmap & (1 << (32 - pid))]
                    elif command == '09 02' and len(payload) >= 20:
                        vin = payload[3:20].decode('ascii', errors='ignore')
                        if re.fullmatch('[A-HJ-NPR-Z0-9]{17}', vin):
                            module['identification'] = {'vin': vin}
                    elif command in ('03', '07', '0A'):
                        faults = decode_dtcs(payload, int(command, 16))
                        module['faults'][command] = {'status': 'ok' if faults is not None else 'invalid', 'codes': faults}
            # Only the existing read PID is used to identify a BMS responder.
            obd.set_header('7E4')
            raw = obd._send_command('21 01', timeout=2)
            report['raw_responses']['7E4:21 01'] = raw
            for ecu, payload in can_payloads(raw).items():
                if payload.startswith(b'\x61\x01') and len(payload) >= 32:
                    report['modules'].setdefault(ecu, {'ecu': ecu, 'name': 'BMS (community PID mapping)',
                        'status': 'responding', 'identification': None,
                        'faults': {'status': 'Hyundai-specific fault protocol not verified'}})
        report['standard_fault_read_available'] = any(
            module.get('faults', {}).get('03', {}).get('status') == 'ok'
            for module in report['modules'].values())
        report['id'] = self.save(report)
        return report

    def prepare_clear(self, obd):
        self.check_parked(obd)
        report = self.read(obd)
        if not report['standard_fault_read_available']:
            raise ValueError('Report saved, but no valid standard stored-fault response; clearing unavailable')
        token = secrets.token_urlsafe(24)
        now = time.time()
        self.pending_clear = {k: v for k, v in self.pending_clear.items() if v['expires'] > now}
        self.pending_clear[token] = {'expires': now + 300, 'connection': obd.conn,
                                     'report_id': report['id']}
        return {'success': True, 'report_id': report['id'], 'confirmation_token': token,
                'expires_at': now + 300, 'message': 'Report saved. Explicit confirmation required before Mode 04.'}

    @staticmethod
    def check_parked(obd):
        sample = obd.query_all().to_dict()
        if not sample['connected'] or sample['speed_kmh'] != 0 or sample['is_charging'] is not False:
            raise ValueError('Fresh stationary, non-charging telemetry required')
        # READY/ignition conditions also require the human confirmation; speed alone cannot prove them.

    def clear(self, obd, payload):
        if payload.get('confirm') is not True or payload.get('conditions_confirmed') is not True:
            raise ValueError('Explicit clearing and vehicle-condition confirmation required')
        pending = self.pending_clear.pop(str(payload.get('confirmation_token', '')), None)
        if not pending or pending['expires'] < time.time() or pending['connection'] is not obd.conn:
            raise ValueError('Confirmation expired or adapter reconnected; save a new report first')
        self.check_parked(obd)
        result = {'created_at': time.time(), 'before_report_id': pending['report_id'],
                  'operation': 'standard_mode_04', 'status': 'sending', 'adapter_mac': obd.conn.mac}
        # Persist intent before sending: a timeout cannot authorize an automatic resend.
        audit_id = self.save(result)
        try:
            with self.headers(obd):
                obd.set_header('7DF')
                raw = obd._send_command('04', timeout=3)
                acknowledged = [ecu for ecu, response in can_payloads(raw).items() if response == b'\x44']
                result.update(raw_response=raw, acknowledged_ecus=acknowledged,
                              status='acknowledged' if acknowledged else 'unconfirmed')
        except Exception as exc:
            result.update(status='unconfirmed', error=str(exc))
        with self.db() as conn:
            conn.execute('UPDATE diagnostic_reports SET body=? WHERE id=?', (json.dumps(result), audit_id))
        return {'success': True, 'audit_id': audit_id, **result,
                'message': 'Mode 04 acknowledged by listed ECUs; reread faults to verify.' if result['status'] == 'acknowledged'
                else 'Clearing result unconfirmed. Reread faults; do not automatically resend.'}

    def expanded(self, obd):
        result = {'profile': PROFILE, 'validation': 'Community definitions; not yet validated on this vehicle',
                  'timestamp': time.time(), 'sources': [PID_SOURCE], 'readings': {}, 'raw_responses': {}}
        bms = {}
        with self.headers(obd):
            obd.set_header('7E4')
            for pid in (1, 2, 3, 4, 5):
                raw = obd._send_command(f'21 {pid:02X}', timeout=2)
                result['raw_responses'][f'7E4:21{pid:02X}'] = raw
                prefix = bytes([0x61, pid])
                responses = [payload[2:] for payload in can_payloads(raw).values() if payload.startswith(prefix)]
                # Never combine bytes from ambiguous/multiple responders.
                if len(responses) == 1:
                    bms[pid] = responses[0]
            obd.set_header('7E2')
            raw = obd._send_command('21 01', timeout=2)
            result['raw_responses']['7E2:2101'] = raw
            vmcu = [payload[2:] for payload in can_payloads(raw).values() if payload.startswith(b'\x61\x01')]
        readings = result['readings']
        cells = []
        for pid in (2, 3, 4):
            data = bms.get(pid, b'')
            cells.extend([round(v / 50, 2) if 140 <= v <= 210 else None for v in data[4:36]] if len(data) >= 36 else [None] * 32)
        readings['cell_voltages_v'] = cells
        readings['cell_imbalance_v'] = round(max(cells) - min(cells), 3) if all(v is not None for v in cells) else None
        data = bms.get(1, b'')
        signed = lambda v: v if v < 128 else v - 256
        temp = lambda v: signed(v) if -40 <= signed(v) <= 80 else None
        readings['module_temperatures_c'] = ([temp(v) for v in data[16:21]] if len(data) >= 21 else [None] * 5)
        second = bms.get(5, b'')
        readings['module_temperatures_c'] += [temp(v) for v in second[9:16]] if len(second) >= 16 else [None] * 7
        readings['inlet_temperature_c'] = temp(data[22]) if len(data) > 22 else None
        readings['cell_max_v'] = data[23] / 50 if len(data) > 23 and 140 <= data[23] <= 210 else None
        readings['cell_min_v'] = data[25] / 50 if len(data) > 25 and 140 <= data[25] <= 210 else None
        readings['motor_rpm'] = int.from_bytes(data[53:55], 'big', signed=True) if len(data) >= 55 else None
        if len(vmcu) == 1 and len(vmcu[0]) >= 13:
            readings['accelerator_pedal_pct'] = vmcu[0][12] / 2 if vmcu[0][12] <= 200 else None
            readings['gear_flags'] = {gear: bool(vmcu[0][5] & (1 << bit)) for bit, gear in enumerate(('P', 'R', 'N', 'D'))}
        else:
            readings['accelerator_pedal_pct'] = readings['gear_flags'] = None
        result['id'] = self.save(result)
        return result
