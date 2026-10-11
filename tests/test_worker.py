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


def tearDownModule():
    _workspace.cleanup()


if __name__ == '__main__':
    unittest.main()
