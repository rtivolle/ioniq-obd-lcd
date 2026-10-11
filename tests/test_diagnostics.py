import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostics import Diagnostics, can_payloads, decode_dtcs


def frames(payload, ecu='7E8'):
    if len(payload) <= 7:
        return ecu + (bytes([len(payload)]) + payload).hex()
    lines = [ecu + (bytes([0x10 | (len(payload) >> 8), len(payload) & 255]) + payload[:6]).hex()]
    for n, offset in enumerate(range(6, len(payload), 7), 1):
        lines.append(ecu + (bytes([0x20 | n % 16]) + payload[offset:offset + 7]).hex())
    return '\n'.join(lines)


class FakeOBD:
    def __init__(self):
        self.conn = type('Connection', (), {'connected': True, 'mac': 'AA:BB:CC:DD:EE:FF', 'close': lambda c: None})()
        self._current_header = ''
        self.commands = []
        self.responses = {
            '01 00': frames(bytes.fromhex('410080000000')),
            '09 00': frames(bytes.fromhex('490040000000')),
            '09 02': frames(b'\x49\x02\x01KMHC75LH1HU123456'),
            '03': frames(bytes.fromhex('43010133')),
            '07': frames(bytes.fromhex('4700')),
            '0A': 'NO DATA',
            '21 01': 'NO DATA',
            '04': frames(b'\x44'),
        }
        self.sample = {'connected': True, 'speed_kmh': 0, 'is_charging': False}

    def set_header(self, header):
        self._current_header = header

    def _send_command(self, command, timeout):
        self.commands.append(command)
        return 'OK' if command.startswith('AT ') else self.responses.get(command, 'NO DATA')

    def query_all(self):
        return type('Sample', (), {'to_dict': lambda obj: self.sample})()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.diag = Diagnostics(str(Path(self.tmp.name) / 'reports.db'))
        self.obd = FakeOBD()

    def tearDown(self):
        self.tmp.cleanup()

    def test_interleaved_iso_tp_and_missing_frame(self):
        payload = bytes(range(20))
        first = frames(payload).splitlines()
        second = frames(payload, '7E9').splitlines()
        raw = '\n'.join(line for pair in zip(first, second) for line in pair)
        self.assertEqual(can_payloads(raw), {'7E8': payload, '7E9': payload})
        self.assertEqual(can_payloads('\n'.join(first[:-1])), {})
        self.assertEqual(can_payloads('\n'.join([first[0], first[2], first[1]])), {})
        self.assertEqual(can_payloads('NO DATA'), {})

    def test_can_dtc_count_and_unknown_definitions(self):
        self.assertEqual(decode_dtcs(bytes.fromhex('4300'), 3), [])
        self.assertIsNone(decode_dtcs(bytes.fromhex('43020133'), 3))
        codes = decode_dtcs(bytes.fromhex('43020133D016'), 3)
        self.assertEqual([c['code'] for c in codes], ['P0133', 'U1016'])
        self.assertIsNotNone(codes[0]['source'])
        self.assertIsNone(codes[1]['source'])

    def test_report_saved_identification_and_partial_fault_coverage(self):
        report = self.diag.read(self.obd)
        self.assertEqual(report['modules']['7E8']['identification']['vin'], 'KMHC75LH1HU123456')
        self.assertTrue(report['standard_fault_read_available'])
        self.assertNotIn('0A', report['modules']['7E8']['faults'])
        self.assertEqual(len(self.diag.reports()), 1)
        self.assertNotIn('04', self.obd.commands)
        self.assertEqual(self.obd.commands[-1], 'AT H0')

    def test_clear_requires_report_confirmation_and_one_use_token(self):
        with self.assertRaises(ValueError):
            self.diag.clear(self.obd, {'confirm': True, 'conditions_confirmed': True})
        preparation = self.diag.prepare_clear(self.obd)
        self.assertNotIn('04', self.obd.commands)
        with self.assertRaises(ValueError):
            self.diag.clear(self.obd, {'confirmation_token': preparation['confirmation_token']})
        payload = {'confirm': True, 'conditions_confirmed': True,
                   'confirmation_token': preparation['confirmation_token']}
        result = self.diag.clear(self.obd, payload)
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(result['acknowledged_ecus'], ['7E8'])
        self.assertEqual(len(self.diag.reports()), 2)
        with self.assertRaises(ValueError):
            self.diag.clear(self.obd, payload)
        self.assertEqual(self.obd.commands.count('04'), 1)

    def test_clear_expiration_reconnection_motion_and_unsupported(self):
        preparation = self.diag.prepare_clear(self.obd)
        payload = {'confirm': True, 'conditions_confirmed': True, 'confirmation_token': preparation['confirmation_token']}
        with patch('diagnostics.time.time', return_value=preparation['expires_at'] + 1):
            with self.assertRaises(ValueError):
                self.diag.clear(self.obd, payload)
        preparation = self.diag.prepare_clear(self.obd)
        payload['confirmation_token'] = preparation['confirmation_token']
        with self.assertRaises(ValueError):
            self.diag.clear(FakeOBD(), payload)
        self.obd.sample['speed_kmh'] = 1
        with self.assertRaises(ValueError):
            self.diag.prepare_clear(self.obd)
        self.obd.sample['speed_kmh'] = 0
        self.obd.responses['03'] = 'NO DATA'
        with self.assertRaises(ValueError):
            self.diag.prepare_clear(self.obd)
        self.assertNotIn('04', self.obd.commands)

    def test_expanded_cells_temperatures_and_missing_blocks(self):
        cell_data = bytes(4) + bytes([180] * 31 + [181])
        for pid in (2, 3, 4):
            self.obd.responses[f'21 {pid:02X}'] = frames(bytes([0x61, pid]) + cell_data, '7EC')
        report = self.diag.expanded(self.obd)
        readings = report['readings']
        self.assertEqual(len(readings['cell_voltages_v']), 96)
        self.assertAlmostEqual(readings['cell_imbalance_v'], .02)
        self.assertEqual(readings['module_temperatures_c'], [None] * 12)
        self.obd.responses['21 03'] = 'NO DATA'
        readings = self.diag.expanded(self.obd)['readings']
        self.assertIsNone(readings['cell_imbalance_v'])
        self.assertEqual(readings['cell_voltages_v'][32:64], [None] * 32)

    def test_unknown_clear_outcome_is_audited(self):
        preparation = self.diag.prepare_clear(self.obd)
        self.obd.responses['04'] = ''
        result = self.diag.clear(self.obd, {'confirm': True, 'conditions_confirmed': True,
                                'confirmation_token': preparation['confirmation_token']})
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertEqual(self.diag.report(result['audit_id'])['status'], 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
