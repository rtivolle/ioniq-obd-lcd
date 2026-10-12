import tempfile
import csv
import io
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ioniq_obd import IoniqOBD
from trip_logger import TripLogger


class FakeConnection:
    connected = True


class PollingTests(unittest.TestCase):
    def adapter(self):
        obd = IoniqOBD(FakeConnection())
        obd.set_header = lambda header: None
        bms = bytearray(55)
        bms[4] = 100
        bms[10:14] = bytes([0, 100, 14, 16])
        soh = bytearray(32)
        soh[31] = 90
        replies = {'01 0D': '410D24', '21 01': '6101' + bms.hex(), '21 05': '6105' + soh.hex()}
        obd._send_command = lambda cmd, timeout: replies.get(cmd, '')
        return obd, replies

    def test_empty_and_no_data_invalidate_and_reconnect(self):
        obd, replies = self.adapter()
        with patch('ioniq_obd.time.time', return_value=100):
            self.assertEqual(obd.query_all().to_dict()['speed_kmh'], 36)
            for n, reply in enumerate(('', 'NO DATA', 'garbage'), 1):
                replies.update(dict.fromkeys(replies, reply))
                data = obd.query_all().to_dict()
                self.assertIsNone(data['speed_kmh'])
                self.assertIsNone(data['battery_power_kw'])
                self.assertEqual(data['status_text'], 'Data unavailable')
                self.assertEqual(obd.reconnect_required, n >= 3)

    def test_partial_poll_optional_lengths_and_zero(self):
        obd, replies = self.adapter()
        replies['01 0D'] = '410D00'
        replies['21 05'] = 'NO DATA'
        with patch('ioniq_obd.time.time', return_value=100):
            data = obd.query_all().to_dict()
            self.assertEqual(data['speed_kmh'], 0)
            self.assertIsNone(data['soc_display'])
            self.assertTrue(data['fresh']['soc_bms'])
        with patch('ioniq_obd.time.time', return_value=111):
            self.assertIsNone(obd.data.to_dict()['speed_kmh'])
        replies['21 01'] = '6101' + bytes(30).hex()
        with patch('ioniq_obd.time.time', return_value=112):
            data = obd.query_all().to_dict()
            self.assertIsNone(data['motor_rpm'])
            self.assertIsNone(data['cum_energy_charged_kwh'])
            self.assertFalse(obd.reconnect_required)

    def test_init_failure_and_header_failure(self):
        obd = IoniqOBD(FakeConnection())
        obd._send_command = lambda *a, **k: ''
        self.assertFalse(obd.initialize_elm327())
        with self.assertRaises(ConnectionError):
            obd.set_header('7E4')
        self.assertEqual(obd._current_header, '')

    def test_missing_speed_and_rpm_remain_unavailable(self):
        from diagnostics import Diagnostics
        obd, replies = self.adapter()
        replies['01 0D'] = 'NO DATA'
        replies['21 01'] = '6101' + bytes(30).hex()
        with patch('ioniq_obd.time.time', return_value=100):
            data = obd.query_all().to_dict()
            self.assertIsNone(data['speed_kmh'])
            self.assertFalse(data['fresh']['speed_kmh'])
            self.assertTrue(data['fresh']['battery_voltage'])
            with self.assertRaises(ValueError):
                Diagnostics.check_parked(obd)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'trips.db')
        self.logger = TripLogger(self.path)

    def tearDown(self):
        self.logger.close()
        self.tmp.cleanup()

    def sample(self, at, speed=36, power=10, charging=False, soc=50, valid=True):
        data = dict(connected=True, speed_kmh=speed, battery_power_kw=power,
                    is_charging=charging, soc_display=soc,
                    fresh=dict.fromkeys(('speed_kmh', 'battery_power_kw', 'is_charging'), valid))
        with patch('trip_logger.time.time', return_value=at):
            self.logger.record_sample(data)

    def test_parked_high_power_is_not_trip(self):
        self.sample(100, speed=0, power=5)
        self.assertIsNone(self.logger.active_trip_id)
        self.assertEqual(self.logger.state, 'parked')

    def test_driving_regen_then_charge_is_separate(self):
        self.sample(100)
        self.sample(110, power=-10, charging=True)
        self.assertAlmostEqual(self.logger.energy_regen_kwh, 10 * 10 / 3600)
        self.assertEqual(self.logger.state, 'driving')
        self.sample(120, speed=0, power=-7, charging=True)
        self.assertIsNone(self.logger.active_trip_id)
        self.sample(130, speed=0, power=-7, charging=True, soc=51)
        self.sample(140, speed=0, power=0)
        charge = self.logger.get_charging_sessions()[0]
        self.assertEqual(charge['status'], 'completed')
        self.assertAlmostEqual(charge['energy_added_kwh'], 7 * 10 / 3600)
        self.assertEqual(charge['duration_sec'], 10)
        self.assertEqual(charge['soc_change'], 1)
        self.assertEqual(len(self.logger.get_all_trips()), 1)

    def test_invalid_gap_never_integrates(self):
        self.sample(100)
        self.sample(110)
        before = self.logger.distance_km
        self.sample(120, valid=False)
        self.sample(130)
        self.assertEqual(self.logger.distance_km, before)
        self.sample(140)
        self.assertAlmostEqual(self.logger.distance_km, before + .1)

    def test_restart_preserves_checkpoint(self):
        self.sample(100)
        self.sample(110)
        recovered = TripLogger(self.path)
        trip = recovered.get_all_trips()[0]
        self.assertEqual(trip['status'], 'interrupted')
        self.assertAlmostEqual(trip['distance_km'], .1)
        self.assertAlmostEqual(trip['energy_used_kwh'], 10 * 10 / 3600)
        self.assertEqual(trip['checkpoint_time'], 110)
        self.assertIsNone(recovered.active_trip_id)

    def test_charge_restart_and_invalid_gap(self):
        self.sample(100, speed=0, power=-7, charging=True)
        self.sample(110, speed=0, power=-7, charging=True)
        self.sample(120, valid=False)
        self.sample(130, speed=0, power=-7, charging=True)
        charge = TripLogger(self.path).get_charging_sessions()[0]
        self.assertEqual(charge['status'], 'interrupted')
        self.assertEqual(charge['duration_sec'], 10)
        self.assertAlmostEqual(charge['energy_added_kwh'], 7 * 10 / 3600)

    def test_manual_trip_idempotent_and_refuses_charging(self):
        self.assertEqual(self.logger.start_trip(), self.logger.start_trip())
        self.sample(100, speed=0, power=-7, charging=True)
        with self.assertRaises(ValueError):
            self.logger.start_trip()

    def test_csv_export_filters_trip_and_preserves_sample_order(self):
        self.sample(100)
        first_id = self.logger.active_trip_id
        self.sample(110, speed=72)
        self.logger.stop_trip()
        self.sample(120, speed=90)
        rows = list(csv.reader(io.StringIO(self.logger.export_trip_csv(first_id))))
        self.assertEqual(len(rows), 3)
        self.assertEqual([float(row[1]) for row in rows[1:]], [36, 72])
        self.assertEqual(len(list(csv.reader(io.StringIO(self.logger.export_trip_csv(-1))))), 1)
        plan = self.logger._get_conn().execute(
            'EXPLAIN QUERY PLAN SELECT * FROM trip_samples WHERE trip_id=? ORDER BY id',
            (first_id,)).fetchall()
        self.assertTrue(any('idx_trip_samples_trip_id' in row[3] for row in plan))

    def test_recording_can_continue_during_csv_export(self):
        self.sample(100)
        trip_id = self.logger.active_trip_id
        exporting, release = threading.Event(), threading.Event()
        errors, exports = [], []
        original_writer = csv.writer

        class PausingWriter:
            def __init__(self, output):
                self.writer = original_writer(output)
            def writerow(self, row):
                return self.writer.writerow(row)
            def writerows(self, rows):
                # Fetch one row so the reader holds a SQLite snapshot.
                self.writer.writerow(next(rows))
                exporting.set()
                if not release.wait(3):
                    raise TimeoutError('CSV export was not released')
                self.writer.writerows(rows)

        def export():
            try:
                exports.append(self.logger.export_trip_csv(trip_id))
            except Exception as exc:
                errors.append(exc)

        def record():
            try:
                self.sample(110, speed=72)
            except Exception as exc:
                errors.append(exc)

        with patch('trip_logger.csv.writer', PausingWriter):
            reader = threading.Thread(target=export)
            reader.start()
            writer = threading.Thread(target=record)
            try:
                self.assertTrue(exporting.wait(2))
                writer.start()
                writer.join(2)
                self.assertFalse(writer.is_alive(), 'Export blocked telemetry recording')
            finally:
                release.set()
                reader.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(list(csv.reader(io.StringIO(exports[0])))), 2)
        self.assertEqual(len(list(csv.reader(io.StringIO(self.logger.export_trip_csv(trip_id))))), 3)


if __name__ == '__main__':
    unittest.main()
