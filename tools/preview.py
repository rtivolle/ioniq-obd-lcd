#!/usr/bin/env python3
"""Preview the dashboard without installing Flask or connecting to hardware.

Run ``python3 tools/preview.py`` and open http://127.0.0.1:5050.
Use ``--port 5051`` to select another port; press Ctrl-C to stop.
All telemetry is sample data, every API response carries a preview marker,
and controls change only this process's memory. Restarting resets the samples.
No application modules, Bluetooth tools, GPIO drivers, or databases are loaded.
"""

import argparse
import copy
import csv
import io
import json
import mimetypes
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
VEHICLE_CATALOG = json.loads((ROOT / 'docs/vehicle/ioniq-2017.json').read_text())


class PreviewState:
    """In-memory fixtures matching the monitor's HTTP API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.desired_settings = {}
        self.config = {
            "bluetooth_mac": "00:1D:A5:68:98:8B",
            "bluetooth_name": "Vgate iCar Pro",
            "bluetooth_pin": "1234",
            "auto_connect": True,
            "lcd_auto_cycle": True,
            "lcd_cycle_seconds": 4,
            "lcd_line1": "soc",
            "lcd_line2": "power",
            "trip_auto_record": True,
            "web_port": 5000,
            "vehicle_model": "Hyundai Ioniq 2017",
            "preview": True,
        }
        self.obd = {
            "connected": True,
            "speed_kmh": 48.0,
            "motor_rpm": 2890,
            "soc_bms": 73.0,
            "soc_display": 76.5,
            "battery_voltage": 357.2,
            "battery_current": 40.0,
            "battery_power_kw": 14.29,
            "battery_max_temp": 28,
            "battery_min_temp": 25,
            "aux_voltage": 14.2,
            "soh": 98.4,
            "max_charge_power_kw": 42.0,
            "max_discharge_power_kw": 88.0,
            "cum_energy_charged_kwh": 1826.8,
            "cum_energy_discharged_kwh": 2034.1,
            "is_charging": False,
            "status_text": "Preview sample data",
            "preview": True,
        }
        self.active_id = 184
        self.next_id = 185
        self.started_at = time.time() - 1432
        self.trip = {
            "active": True,
            "state": "driving",
            "trip_id": self.active_id,
            "charging_id": None,
            "distance_km": 18.42,
            "energy_used_kwh": 2.73,
            "energy_regen_kwh": 0.46,
            "net_kwh": 2.27,
            "max_speed_kmh": 82.0,
            "efficiency_kwh_100km": 12.3,
            "start_soc": 84.0,
            "current_soc": 76.5,
            "preview": True,
        }
        now = datetime.now()
        self.trips = [
            self._history(184, now - timedelta(minutes=24), 18.42, 2.27, 0.46, 82, 84, 76.5, "active"),
            self._history(183, now - timedelta(days=1, hours=2), 34.8, 4.59, 0.72, 104, 91, 74, "completed"),
            self._history(182, now - timedelta(days=2, hours=3), 12.6, 1.58, 0.31, 67, 78, 72, "completed"),
        ]
        self.lcd = {"line1": "SOC:76.5% +14.3k", "line2": "Spd: 48k 357.2V ", "preview": True}
        self.lcd_override_until = 0.0

    @staticmethod
    def _history(trip_id, start, distance, energy, regen, speed, soc_start, soc_end, status):
        return {
            "id": trip_id,
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None if status == "active" else (start + timedelta(minutes=32)).strftime("%Y-%m-%d %H:%M:%S"),
            "distance_km": distance,
            "energy_used_kwh": round(energy + regen, 2),
            "energy_regen_kwh": regen,
            "net_kwh": energy,
            "max_speed_kmh": speed,
            "avg_speed_kmh": 42.0,
            "start_soc": soc_start,
            "end_soc": soc_end,
            "status": status,
            "preview": True,
        }

    def status(self):
        obd = copy.deepcopy(self.obd)
        now = time.time()
        obd["timestamp"] = now if obd["connected"] else 0.0
        keys = [key for key, value in obd.items() if isinstance(value, (int, float, bool)) and key not in {"connected", "preview", "timestamp"}]
        obd["fresh"] = dict.fromkeys(keys, obd["connected"])
        obd["metric_updated_at"] = dict.fromkeys(keys, now if obd["connected"] else 0.0)
        if not obd["connected"]:
            for key in keys:
                obd[key] = None
        trip = dict(self.trip)
        trip["duration_sec"] = int(now - self.started_at) if self.active_id else 0
        if now >= self.lcd_override_until:
            if not obd["connected"]:
                self.lcd.update(line1="OBD2 Waiting...  ", line2="Preview offline ")
            else:
                self.lcd.update(getattr(self, "default_lcd", {"line1": "SOC:76.5% +14.3k", "line2": "Spd: 48k 357.2V "}))
        return {"preview": True, "obd": obd, "trip": trip, "config": dict(self.config), "lcd": dict(self.lcd)}

    def devices(self):
        return [
            {"name": "Vgate iCar Pro", "mac": "00:1D:A5:68:98:8B", "rssi": "-54", "paired": True, "connected": self.obd["connected"], "preview": True},
            {"name": "<img src=x onerror=\"window.__ioniqUnsafeName=true\"> ELM327's & \"OBD\"", "mac": "AA:BB:CC:DD:EE:FF", "rssi": "-71", "paired": False, "connected": False, "preview": True},
        ]

    def vehicle_settings(self):
        return {'preview': True, 'profile': VEHICLE_CATALOG['profile'], 'settings': {
            key: {**definition, 'desired_value': self.desired_settings.get(key),
                  'vehicle_value': None, 'read_supported': False, 'write_supported': False,
                  'status': 'pending_protocol' if key in self.desired_settings else 'unsupported'}
            for key, definition in VEHICLE_CATALOG['persistent_settings'].items()}}

    def mutate(self, path, payload):
        if path == '/api/vehicle/settings':
            if not payload:
                raise ValueError('Provide at least one desired setting')
            for key, value in payload.items():
                definition = VEHICLE_CATALOG['persistent_settings'].get(key)
                if not definition:
                    raise ValueError('Unknown setting')
                if definition['type'] == 'integer':
                    if type(value) is not int or not definition['min'] <= value <= definition['max']:
                        raise ValueError('Invalid desired charge limit')
                elif value not in definition['choices']:
                    raise ValueError('Invalid desired preference')
            self.desired_settings.update(payload)
            return {'success': True, 'message': 'Preview request saved in memory only. Not applied to vehicle.', **self.vehicle_settings()}
        if path.startswith('/api/vehicle/'):
            raise ValueError('Vehicle operations unavailable in sample-data preview')
        result = {"success": True, "preview": True}
        if path == "/api/config":
            self.config.update({key: value for key, value in payload.items() if key in self.config and key != "preview"})
            result["config"] = dict(self.config)
        elif path == "/api/trips/start":
            if not self.active_id:
                self.active_id = self.next_id
                self.next_id += 1
                self.started_at = time.time()
                self.trip.update(active=True, state="driving", trip_id=self.active_id, distance_km=0.0,
                                 energy_used_kwh=0.0, energy_regen_kwh=0.0, net_kwh=0.0,
                                 max_speed_kmh=0.0, efficiency_kwh_100km=0.0, start_soc=76.5)
                self.trips.insert(0, self._history(self.active_id, datetime.now(), 0, 0, 0, 0, 76.5, 76.5, "active"))
            result["trip_id"] = self.active_id
        elif path == "/api/trips/stop":
            result["finished_trip_id"] = self.active_id
            for trip in self.trips:
                if trip["id"] == self.active_id:
                    trip["status"] = "completed"
                    trip["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.active_id = None
            self.trip.update(active=False, state="parked", trip_id=None)
        elif path == "/api/lcd/message":
            message = str(payload.get("message", "")).replace("\\n", "\n")
            lines = message.split("\n")
            self.lcd.update(line1=lines[0][:16].ljust(16), line2=(lines[1] if len(lines) > 1 else "")[:16].ljust(16))
            self.lcd_override_until = time.time() + max(0, float(payload.get("duration", 5)))
            result["message"] = "Preview LCD message displayed."
        elif path == "/api/lcd/clear":
            self.lcd.update(line1=" " * 16, line2=" " * 16)
            self.lcd_override_until = time.time() + 1
        elif path == "/api/bluetooth/connect":
            mac = str(payload.get("mac", "")).strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                raise ValueError("A valid Bluetooth MAC address is required.")
            self.config.update(bluetooth_mac=mac, bluetooth_name=str(payload.get("name", "")) or mac,
                               bluetooth_pin=str(payload.get("pin", "1234")), auto_connect=True)
            self.obd.update(connected=True, status_text="Preview sample data")
            result["message"] = "Preview adapter connected and remembered."
        elif path == "/api/bluetooth/reconnect":
            if not self.config["bluetooth_mac"]:
                raise ValueError("No device remembered.")
            self.obd.update(connected=True, status_text="Preview sample data")
            result["message"] = "Preview adapter reconnected."
        elif path in {"/api/bluetooth/disconnect", "/api/bluetooth/forget"}:
            self.obd.update(connected=False, status_text="Preview adapter disconnected")
            if path.endswith("forget"):
                self.config.update(bluetooth_mac="", bluetooth_name="")
            result["message"] = "Preview adapter forgotten." if path.endswith("forget") else "Preview adapter disconnected."
        else:
            return None
        return result


class PreviewHandler(BaseHTTPRequestHandler):
    state = PreviewState()

    def send_body(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Ioniq-Preview", "true")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        with self.state.lock:
            if path == "/api/status":
                return self.send_json(self.state.status())
            if path == "/api/config":
                return self.send_json(dict(self.state.config))
            if path == '/api/vehicle/capabilities':
                return self.send_json({**VEHICLE_CATALOG, 'preview': True})
            if path == '/api/vehicle/settings':
                return self.send_json(self.state.vehicle_settings())
            if path == "/api/trips":
                return self.send_json(self.state.trips)
            if path == "/api/charging":
                return self.send_json([])
            if path == "/api/bluetooth/scan":
                return self.send_json(self.state.devices())
            csv_route = re.fullmatch(r"/api/trips/(\d+)/csv", path)
            if csv_route:
                trip_id = int(csv_route[1])
                trip = next((item for item in self.state.trips if item["id"] == trip_id), None)
                if trip is None:
                    return self.send_json({"success": False, "preview": True, "message": "Preview trip not found."}, 404)
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["Timestamp", "Speed_kmh", "Power_kW", "Voltage_V", "Current_A", "SOC_pct", "Aux_V", "Battery_Temp_C"])
                writer.writerow([trip["start_time"], 48, 14.29, 357.2, 40, trip["start_soc"], 14.2, 28])
                return self.send_body(output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", filename=f"ioniq_preview_trip_{trip_id}.csv")
        if path == "/":
            file_path = ROOT / "templates" / "index.html"
        elif path.startswith("/static/"):
            static_root = (ROOT / "static").resolve()
            file_path = (ROOT / path.lstrip("/")).resolve()
            if not file_path.is_relative_to(static_root):
                return self.send_json({"success": False, "preview": True, "message": "Not found."}, 404)
        else:
            return self.send_json({"success": False, "preview": True, "message": "Not found."}, 404)
        if not file_path.is_file():
            return self.send_json({"success": False, "preview": True, "message": "Not found."}, 404)
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_body(file_path.read_bytes(), mime)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError("Request body is too large.")
            payload = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object.")
            with self.state.lock:
                result = self.state.mutate(urlsplit(self.path).path, payload)
                if result is None:
                    return self.send_json({"success": False, "preview": True, "message": "Not found."}, 404)
                self.send_json(result)
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            self.send_json({"success": False, "preview": True, "message": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=5050, help="Local preview port (default: 5050)")
    parser.add_argument("--scenario", choices=("driving", "zero", "regen", "charging", "disconnected", "empty"),
                        default="driving", help="Sample state for UI checks (default: driving)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    state = PreviewHandler.state
    if args.scenario != "driving":
        state.mutate("/api/trips/stop", {})
    if args.scenario == "zero":
        state.obd.update(speed_kmh=0.0, motor_rpm=0, soc_display=0.0, soc_bms=0.0,
                         battery_power_kw=0.0, battery_current=0.0, soh=0.0,
                         battery_min_temp=0, battery_max_temp=0)
    elif args.scenario == "regen":
        state.obd.update(battery_power_kw=-12.8, battery_current=-35.8)
    elif args.scenario == "charging":
        state.obd.update(speed_kmh=0.0, motor_rpm=0, is_charging=True,
                         battery_power_kw=-6.8, battery_current=-19.0)
        state.trip.update(state="charging", charging_id=1)
    elif args.scenario == "disconnected":
        state.obd.update(connected=False, status_text="Preview adapter disconnected")
    elif args.scenario == "empty":
        state.trips.clear()
    # The LCD fixture matches the selected scenario; custom messages still override it.
    state.default_lcd = {
        "line1": f"SOC:{state.obd['soc_display']:4.1f}%".ljust(16),
        "line2": f"Pwr:{state.obd['battery_power_kw']:+5.1f}kW".ljust(16),
    }
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Sample-data preview: http://127.0.0.1:{args.port} (controls are in memory only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
