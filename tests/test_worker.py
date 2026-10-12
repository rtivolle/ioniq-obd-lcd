import os
import tempfile
import threading
import unittest
from unittest.mock import patch

# Avoid creating production DB or touching GPIO on import.
_workspace = tempfile.TemporaryDirectory()
os.environ['IONIQ_DB_PATH'] = os.path.join(_workspace.name, 'trips.db')
with patch('lcd_driver.LCDDriver._init_lcd'):
    import main


class WorkerTests(unittest.TestCase):
    def test_poll_publishes_live_readings_and_disconnect_invalidates_them(self):
        from ioniq_obd import IoniqData, METRICS
        import time
        sample = IoniqData(connected=True, speed_kmh=36, battery_voltage=360)
        sample.metric_valid = dict.fromkeys(METRICS, True)
        sample.metric_updated_at = dict.fromkeys(METRICS, time.time())
        responses = []

        class Connection:
            connected = True
            def close(self):
                self.connected = False

        class OBD:
            reconnect_required = False
            def query_all(self):
                return sample

        def inspect_poll(data, **kwargs):
            responses.append(main.app.test_client().get('/api/status').json['obd'])
            main.running = False

        main.running = True
        main.bt_conn, main.live_obd = Connection(), OBD()
        with patch.object(main.trip_logger, 'record_sample', side_effect=inspect_poll):
            main.obd_worker()
        self.assertEqual(responses[0]['speed_kmh'], 36)
        self.assertTrue(responses[0]['fresh']['speed_kmh'])
        self.assertIsNone(responses[-1]['speed_kmh'])
        self.assertFalse(responses[-1]['connected'])

    def test_status_expires_readings_and_scan_invalidates_snapshot(self):
        from ioniq_obd import IoniqData, METRICS
        sample = IoniqData(connected=True, speed_kmh=36)
        sample.metric_valid = dict.fromkeys(METRICS, True)
        sample.metric_updated_at = dict.fromkeys(METRICS, 100)
        with patch.object(main, 'current_data', sample):
            with patch('ioniq_obd.time.time', return_value=111):
                self.assertIsNone(main.app.test_client().get('/api/status').json['obd']['speed_kmh'])
            with patch('ioniq_obd.time.time', return_value=100), patch.object(main.BluetoothManager, 'scan_devices', return_value=[]):
                main.handle_worker_request('scan', {})
                self.assertIsNone(main.app.test_client().get('/api/status').json['obd']['speed_kmh'])

    def test_requests_run_on_worker_not_http_thread(self):
        calls = []
        def capture(action, payload):
            calls.append((action, threading.current_thread().name))
            return [] if action == 'scan' else {'success': True}
        main.running = True
        main.manual_disconnect = True
        with patch.object(main, 'handle_worker_request', side_effect=capture):
            worker = threading.Thread(target=main.obd_worker, name='TestOBDWorker')
            worker.start()
            self.assertTrue(main.worker_ready.wait(2))
            try:
                client = main.app.test_client()
                for route, payload in [('connect', {'mac': 'AA:BB:CC:DD:EE:FF', 'name': '<img>', 'pin': '1234'}),
                                       ('disconnect', {}), ('forget', {})]:
                    self.assertEqual(client.post('/api/bluetooth/' + route, json=payload).status_code, 200)
                self.assertEqual(client.get('/api/bluetooth/scan').json, [])
                for route in ('read', 'controls/prepare', 'controls/execute', 'settings/capture'):
                    self.assertEqual(client.post('/api/vehicle/' + route, json={}).status_code, 200)
                self.assertTrue(all(name == 'TestOBDWorker' for _, name in calls))
            finally:
                main.running = False
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertFalse(main.worker_ready.is_set())

    def test_disconnect_waits_for_in_flight_poll(self):
        from ioniq_obd import IoniqData
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        owners = []
        class Connection:
            connected = True
            def close(self):
                owners.append(threading.current_thread().name)
                closed.set()
        class OBD:
            reconnect_required = False
            def query_all(self):
                entered.set()
                release.wait(3)
                return IoniqData(connected=True)
        main.running = True
        main.bt_conn, main.live_obd = Connection(), OBD()
        worker = threading.Thread(target=main.obd_worker, name='ExclusiveOBDWorker')
        response = []
        request_thread = threading.Thread(target=lambda: response.append(
            main.app.test_client().post('/api/bluetooth/disconnect')))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            request_thread.start()
            self.assertFalse(closed.wait(.1))
            release.set()
            request_thread.join(2)
            self.assertFalse(request_thread.is_alive())
            self.assertEqual(response[0].status_code, 200)
            self.assertTrue(closed.is_set())
            self.assertEqual(owners, ['ExclusiveOBDWorker'])
        finally:
            release.set()
            main.running = False
            worker.join(2)

    def test_unavailable_worker_and_invalid_mac(self):
        client = main.app.test_client()
        self.assertEqual(client.post('/api/bluetooth/disconnect').status_code, 503)
        self.assertEqual(client.post('/api/bluetooth/connect', json={'mac': 'bad'}).status_code, 400)

    def test_status_and_manual_trip_requires_fresh_readings(self):
        client = main.app.test_client()
        self.assertEqual(client.get('/api/status').status_code, 200)
        self.assertIsNone(client.get('/api/status').json['obd']['speed_kmh'])
        self.assertEqual(client.post('/api/trips/start').status_code, 409)
        self.assertEqual(client.get('/api/charging').json, [])
        self.assertEqual(client.get('/').status_code, 200)

    def test_config_cannot_switch_adapter_outside_worker(self):
        client = main.app.test_client()
        self.assertEqual(client.post('/api/config', json={'bluetooth_mac': 'AA:BB:CC:DD:EE:FF'}).status_code, 400)

    def test_vehicle_settings_api_stores_requests_but_refuses_vehicle_write(self):
        client = main.app.test_client()
        self.assertEqual(client.get('/api/vehicle/capabilities').status_code, 200)
        self.assertEqual(client.post('/api/vehicle/settings', json={'auto_lock':'at_speed'}).status_code, 200)
        self.assertEqual(client.get('/api/vehicle/settings').json['settings']['auto_lock']['desired_value'], 'at_speed')
        with patch.object(main, 'submit_worker_request') as queued:
            self.assertEqual(client.post('/api/vehicle/settings/auto_lock/apply').status_code, 409)
            queued.assert_not_called()
        self.assertEqual(client.post('/api/vehicle/settings', json={'ac_charge_limit_pct':True}).status_code, 400)


def tearDownModule():
    _workspace.cleanup()


if __name__ == '__main__':
    unittest.main()
