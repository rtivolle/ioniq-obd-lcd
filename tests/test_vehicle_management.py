import tempfile
import unittest
from unittest.mock import patch

from diagnostics import Diagnostics
from vehicle_management import VehicleManagement


class Connection:
    connected = True
    mac = 'AA:BB:CC:DD:EE:FF'
    def close(self):
        self.connected = False


class FakeOBD:
    def __init__(self):
        self.conn = Connection()
        self.commands = []
        self._current_header = ''
        self.header = '770'
        self.bad_command = None
        self.read_value = 1
    def set_header(self, header):
        self.header = header
    def _send_command(self, command, timeout):
        self.commands.append(command)
        if command == self.bad_command:
            return 'NO DATA'
        if command.startswith('AT '):
            return 'OK' if self.conn.connected else ''
        request = bytes.fromhex(command.replace(' ', ''))
        payload = bytes([request[0] + 0x40]) + request[1:]
        if request[0] in (0x21, 0x22):
            payload += bytes([self.read_value])
        return f'{int(self.header,16)+8:03X}{len(payload):02X}{payload.hex()}'


class VehicleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.diagnostics = Diagnostics(self.tmp.name + '/test.db')
        self.vehicle = VehicleManagement(self.diagnostics)
        self.obd = FakeOBD()
        self.guard = patch.object(Diagnostics, 'check_parked')
        self.guard.start()
    def tearDown(self):
        self.guard.stop()
        self.tmp.cleanup()
    def prepare(self, **changes):
        payload = dict(ecu='IGPM', control='BC01', vehicle_state='ACC',
                       profile_confirmed=True, conditions_confirmed=True)
        payload.update(changes)
        return self.vehicle.prepare(self.obd, payload)
    def execute(self, preparation):
        with patch('vehicle_management.time.sleep'):
            return self.vehicle.execute(self.obd, dict(
                confirmation_token=preparation['confirmation_token'], confirm=True, conditions_confirmed=True))

    def test_catalog_and_allowlist(self):
        catalog = self.vehicle.catalog()
        self.assertEqual(len(catalog['ecus']), 31)
        self.assertNotIn('activate', catalog['ecus']['IGPM']['controls']['BC01'])
        for ecu, pid in [('IGPM','2FBC0103'), ('IGPM','22FFFF'), ('unknown','2101')]:
            with self.assertRaises(ValueError):
                self.vehicle.read(self.obd, ecu, pid)
        for changes in [dict(ecu='PSM', control='B401'), dict(ecu='SKM',control='B108'),
                        dict(control='FFFF'), dict(vehicle_state='READY'), dict(profile_confirmed=False),
                        dict(conditions_confirmed=False)]:
            with self.assertRaises(ValueError): self.prepare(**changes)
        self.assertEqual(self.obd.commands, [])

    def test_single_read_and_restoration(self):
        report = self.vehicle.read(self.obd, 'IGPM', '22BC03')
        self.assertEqual(report['status'], 'responding')
        self.assertTrue(report['payload_hex'].startswith('62BC03'))
        self.assertIn('AT FC SH 770', self.obd.commands)
        self.assertEqual(self.obd.commands[-2:], ['AT FC SM 0','AT H0'])
        self.assertEqual(self.diagnostics.report(report['id'])['ecu'], 'IGPM')

    def test_missing_read_is_not_clean_data(self):
        self.obd.bad_command = '22BC03'
        report = self.vehicle.read(self.obd, 'IGPM', '22BC03')
        self.assertEqual(report['status'], 'unavailable')
        self.assertNotIn('payload_hex', report)

    def test_activation_releases_and_audits_then_rejects_replay(self):
        preparation = self.prepare()
        result = self.execute(preparation)
        self.assertEqual(result['status'], 'acknowledged')
        commands = self.obd.commands
        self.assertLess(commands.index('10 03'), commands.index('2FBC0103'))
        self.assertLess(commands.index('2FBC0103'), commands.index('2FBC0100'))
        self.assertLess(commands.index('2FBC0100'), commands.index('10 01'))
        self.assertEqual(self.diagnostics.report(result['audit_id'])['status'], 'acknowledged')
        with self.assertRaises(ValueError): self.execute(preparation)
        self.assertEqual(commands.count('2FBC0103'), 1)

    def test_lost_activation_reply_still_releases_without_retry(self):
        self.obd.bad_command = '2FBC0103'
        result = self.execute(self.prepare())
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertIn('2FBC0100', self.obd.commands)
        self.assertIn('10 01', self.obd.commands)
        self.assertEqual(self.obd.commands.count('2FBC0103'), 1)
        self.assertEqual(result['raw_responses']['2FBC0103'], 'NO DATA')

    def test_reconnection_expiry_motion_and_release_failure(self):
        preparation = self.prepare()
        self.obd.conn = Connection()
        with self.assertRaises(ValueError): self.execute(preparation)
        preparation = self.prepare()
        with patch('vehicle_management.time.time', return_value=preparation['expires_at']):
            with self.assertRaises(ValueError): self.execute(preparation)
        preparation = self.prepare()
        with patch.object(Diagnostics, 'check_parked', side_effect=ValueError('Moving')):
            with self.assertRaises(ValueError): self.execute(preparation)
        self.assertEqual(self.obd.commands, [])
        self.obd.bad_command = '2FBC0100'
        result = self.execute(self.prepare())
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertFalse(self.obd.conn.connected)
        self.assertIn('10 01', self.obd.commands)

    def test_settings_persist_but_never_send_vehicle_commands(self):
        result = self.vehicle.save_desired({'ac_charge_limit_pct':80,'auto_lock':'at_speed'})
        self.assertIn('Not applied',result['message'])
        restored = VehicleManagement(self.diagnostics).settings()['settings']
        self.assertEqual(restored['ac_charge_limit_pct']['desired_value'],80)
        self.assertEqual(restored['auto_lock']['status'],'pending_protocol')
        self.assertIsNone(restored['auto_lock']['vehicle_value'])
        for key in restored:
            self.assertFalse(restored[key]['write_supported'])
            with self.assertRaises(ValueError): self.vehicle.apply_setting(key)
        self.assertEqual(self.obd.commands, [])

    def test_setting_validation_is_atomic(self):
        for values in ({}, {'unknown':1}, {'ac_charge_limit_pct':True}, {'dc_charge_limit_pct':0},
                       {'ac_charge_limit_pct':80.5}, {'auto_lock':'anything'},
                       {'ac_charge_limit_pct':80,'auto_unlock':'invalid'}):
            with self.assertRaises(ValueError): self.vehicle.save_desired(values)
        self.assertIsNone(self.vehicle.settings()['settings']['ac_charge_limit_pct']['desired_value'])

    def test_setting_capture_compares_raw_bytes_without_writes(self):
        before = self.vehicle.capture_setting(self.obd, {'setting': 'ac_charge_limit_pct'})
        self.obd.read_value = 2
        after = self.vehicle.capture_setting(self.obd, {'setting': 'ac_charge_limit_pct', 'before_report_id': before['id']})
        self.assertEqual(after['phase'], 'after')
        self.assertEqual(after['changed_payload_bytes']['VCU:21F2'], [
            {'isotp_index': 2, 'before': 1, 'after': 2}])
        self.assertFalse(any(command.startswith(('2E', '2F', '31', '27')) for command in self.obd.commands))
        with self.assertRaises(ValueError):
            self.vehicle.capture_setting(self.obd, {'setting': 'auto_lock', 'before_report_id': before['id']})
