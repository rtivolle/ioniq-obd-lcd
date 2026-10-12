import io
import unittest
from unittest.mock import patch

import readout
from ioniq_obd import METRICS


class ReadoutTests(unittest.TestCase):
    def run_readout(self, report):
        replies = [
            {'obd': dict.fromkeys(METRICS, None)},
            {'readings': {'accelerator_pedal_pct': None, 'motor_rpm': None}},
            report,
        ]
        output = io.StringIO()
        with patch.object(readout, 'fetch_api', side_effect=replies), patch(
            'sys.argv', ['readout.py']
        ), patch('sys.stdout', output):
            readout.main()
        return output.getvalue()

    def test_bms_metadata_and_missing_metrics_do_not_crash(self):
        output = self.run_readout({'modules': {
            '7EC': {'faults': {'status': 'Hyundai-specific fault protocol not verified'}},
            '7E8': {'faults': {'03': {'status': 'ok', 'codes': [
                {'code': 'P0133', 'description': 'Example fault'}]}}},
        }})
        self.assertIn('Unavailable', output)
        self.assertIn('P0133', output)
        self.assertIn('7EC Mode 03: unavailable', output)
        self.assertIn('Hyundai-specific fault protocol not verified', output)
        self.assertNotIn('across all ECUs', output)

    def test_no_responders_are_reported_as_unavailable(self):
        output = self.run_readout({'modules': {}})
        self.assertIn('no responding ECUs', output)
        self.assertNotIn('No fault codes', output)

    def test_bms_only_does_not_claim_a_clean_fault_read(self):
        output = self.run_readout({'modules': {'7EC': {'faults': {
            'status': 'Hyundai-specific fault protocol not verified'}}}})
        self.assertIn('no valid standard OBD responses', output)
        self.assertNotIn('No fault codes', output)
