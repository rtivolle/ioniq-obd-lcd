"""Main entry point and orchestrator for Hyundai Ioniq OBD2 & 1602A LCD System.
Runs Bluetooth manager, OBD2/Simulator polling loop, LCD display loop, trip logger,
and Flask web service.
"""

import logging
import queue
import copy
import re
from concurrent.futures import Future, TimeoutError
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, render_template, request

# Import local modules
from bluetooth_manager import BluetoothManager, BluetoothSerialConnection
from config import load_config, save_config
from ioniq_obd import IoniqData, IoniqOBD
from lcd_driver import LCDDriver
from trip_logger import TripLogger
from diagnostics import Diagnostics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ioniq_main")

# State & objects
config = load_config()
lcd = LCDDriver()
trip_logger = TripLogger(os.environ.get("IONIQ_DB_PATH", "/home/rtivolle/ioniq_obd/trips.db"))

diagnostics = Diagnostics(trip_logger.db_path)

bt_conn: Optional[BluetoothSerialConnection] = None
live_obd: Optional[IoniqOBD] = None
current_data: IoniqData = IoniqData(connected=False, status_text="Starting OBD...")
cached_obd_dict: Dict[str, Any] = current_data.to_dict()
data_lock = threading.Lock()
running = True
worker_requests = queue.Queue()
worker_ready = threading.Event()
manual_disconnect = False

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))


# -------------------------------------------------------------
# Background Workers
# -------------------------------------------------------------

def close_connection():
    """Called exclusively by the OBD worker."""
    global bt_conn, live_obd, current_data
    if bt_conn:
        bt_conn.close()
    bt_conn = live_obd = None
    with data_lock:
        current_data = IoniqData(status_text="Data unavailable")
    trip_logger.record_sample(current_data.to_dict())


def connect_adapter(mac):
    global bt_conn, live_obd
    close_connection()
    channel = config.get("bluetooth_channel", 2)
    conn = BluetoothSerialConnection(mac=mac, channel=channel, timeout=4.0)
    if conn.connect():
        obd = IoniqOBD(conn)
        if obd.initialize_elm327():
            bt_conn, live_obd = conn, obd
            return True
    conn.close()
    return False


def handle_worker_request(action, payload):
    global manual_disconnect
    if action == "scan" or action.startswith("diagnostic_"):
        with data_lock:
            current_data.metric_valid = {}
            cached_obd_dict = current_data.to_dict()
        trip_logger.record_sample(cached_obd_dict)
        if action == "scan":
            return BluetoothManager.scan_devices(timeout=5)
        if live_obd is None or bt_conn is None or not bt_conn.connected:
            raise ConnectionError("Connect an adapter before running diagnostics")
        if action == "diagnostic_read":
            return diagnostics.read(live_obd)
        if action == "diagnostic_expanded":
            return diagnostics.expanded(live_obd)
        if action == "diagnostic_prepare_clear":
            return diagnostics.prepare_clear(live_obd)
        if action == "diagnostic_clear":
            return diagnostics.clear(live_obd, payload)
        raise ValueError("Unsupported diagnostic operation")
    close_connection()
    if action in ("disconnect", "forget"):
        manual_disconnect = True
        if action == "forget":
            mac = config.get("bluetooth_mac", "")
            if mac:
                BluetoothManager.remove_device(mac)
            config.update(bluetooth_mac="", bluetooth_name="")
            save_config(config)
        return {"success": True, "message": "Disconnected" if action == "disconnect" else "Remembered device cleared"}
    manual_disconnect = False
    if action == "connect":
        BluetoothManager.pair_and_trust(mac=payload["mac"], pin=payload["pin"])
        config.update(bluetooth_mac=payload["mac"], bluetooth_name=payload["name"] or payload["mac"],
                      bluetooth_pin=payload["pin"], auto_connect=True)
        save_config(config)
    mac = config.get("bluetooth_mac", "").strip()
    success = bool(mac) and connect_adapter(mac)
    return {"success": success, "message": "Connected and remembered" if success else "Could not connect to adapter"}


def submit_worker_request(action, payload=None):
    if not worker_ready.is_set():
        return jsonify({"success": False, "message": "OBD worker unavailable"}), 503
    future = Future()
    worker_requests.put((action, payload or {}, future))
    try:
        return jsonify(future.result(timeout=55))
    except TimeoutError:
        if future.cancel():
            message = "Request cancelled before execution"
        else:
            message = "Bluetooth operation still running; check status"
        return jsonify({"success": False, "message": message}), 503
    except Exception as exc:
        logger.exception("Bluetooth operation failed")
        return jsonify({"success": False, "message": str(exc)}), 503


def obd_worker():
    """Sole owner of pairing, connection lifecycle and adapter I/O."""
    global current_data
    worker_ready.set()
    retry_at = 0.0
    try:
        while running:
            try:
                action, payload, future = worker_requests.get(timeout=0.3)
            except queue.Empty:
                pass
            else:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(handle_worker_request(action, payload))
                    except Exception as exc:
                        future.set_exception(exc)
                retry_at = time.monotonic() + 3
                continue
            mac = config.get("bluetooth_mac", "").strip()
            if bt_conn is None:
                if manual_disconnect or not mac or not config.get("auto_connect", True):
                    with data_lock:
                        current_data.status_text = "Disconnected"
                        cached_obd_dict = current_data.to_dict()
                    continue
                if time.monotonic() < retry_at:
                    continue
                try:
                    with data_lock:
                        current_data.status_text = "Connecting..."
                        cached_obd_dict = current_data.to_dict()
                    connect_adapter(mac)
                except Exception:
                    logger.exception("BT connect error")
                    close_connection()
                    with data_lock:
                        current_data.status_text = "Connect failed"
                        cached_obd_dict = current_data.to_dict()
                retry_at = time.monotonic() + 3
                if live_obd is None:
                    continue
            try:
                polled = live_obd.query_all()
                polled_dict = polled.to_dict()
                with data_lock:
                    current_data = polled.clone()
                    cached_obd_dict = polled_dict
                trip_logger.record_sample(polled_dict, auto_manage=config.get("trip_auto_record", True))
                if live_obd.reconnect_required:
                    close_connection()
                    retry_at = time.monotonic() + 3
                else:
                    time.sleep(0.15)
            except Exception:
                logger.exception("OBD poll error")
                close_connection()
                retry_at = time.monotonic() + 3
    finally:
        worker_ready.clear()
        close_connection()


def lcd_worker():
    """Background worker to continuously update the 1602A LCD screen."""
    global running
    while running:
        try:
            with data_lock:
                obd_dict = cached_obd_dict
            trip_dict = trip_logger.get_active_trip_summary()
            lcd.update_from_data(obd_dict, trip_dict, config)
        except Exception as e:
            logger.error("LCD update worker error: %s", e)
        time.sleep(0.5)


# -------------------------------------------------------------
# Web API Routes
# -------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with data_lock:
        obd_dict = cached_obd_dict
    trip_dict = trip_logger.get_active_trip_summary()
    lcd_mirror = {
        "line1": lcd.last_line1,
        "line2": lcd.last_line2
    }
    return jsonify({
        "obd": obd_dict,
        "trip": trip_dict,
        "config": config,
        "lcd": lcd_mirror
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global config
    if request.method == "POST":
        new_data = request.json or {}
        if any(key in new_data for key in ("bluetooth_mac", "bluetooth_name", "bluetooth_pin")):
            return jsonify({"success": False, "message": "Use Bluetooth connect/forget endpoints to change the device"}), 400
        config.update(new_data)
        save_config(config)
        return jsonify({"success": True, "config": config})
    return jsonify(config)


@app.route("/api/bluetooth/scan")
def api_bluetooth_scan():
    return submit_worker_request("scan")


@app.route("/api/bluetooth/connect", methods=["POST"])
def api_bluetooth_connect():
    data = request.json or {}
    mac = str(data.get("mac", "")).strip().upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
        return jsonify({"success": False, "message": "Invalid MAC address"}), 400
    return submit_worker_request("connect", {
        "mac": mac, "name": str(data.get("name", "")).strip(),
        "pin": str(data.get("pin", "1234")).strip()})


@app.route("/api/bluetooth/reconnect", methods=["POST"])
def api_bluetooth_reconnect():
    if not config.get("bluetooth_mac"):
        return jsonify({"success": False, "message": "No device remembered"}), 400
    return submit_worker_request("reconnect")


@app.route("/api/bluetooth/disconnect", methods=["POST"])
def api_bluetooth_disconnect():
    return submit_worker_request("disconnect")


@app.route("/api/bluetooth/forget", methods=["POST"])
def api_bluetooth_forget():
    return submit_worker_request("forget")


@app.route("/api/diagnostics/read", methods=["POST"])
def api_diagnostic_read():
    return submit_worker_request("diagnostic_read")


@app.route("/api/diagnostics/expanded", methods=["POST"])
def api_diagnostic_expanded():
    return submit_worker_request("diagnostic_expanded")


@app.route("/api/diagnostics/prepare-clear", methods=["POST"])
def api_diagnostic_prepare_clear():
    return submit_worker_request("diagnostic_prepare_clear")


@app.route("/api/diagnostics/clear", methods=["POST"])
def api_diagnostic_clear():
    data = request.json or {}
    if data.get("confirm") is not True or data.get("conditions_confirmed") is not True:
        return jsonify({"success": False, "message": "Explicit confirmation required"}), 400
    return submit_worker_request("diagnostic_clear", data)


@app.route("/api/diagnostics/reports")
def api_diagnostic_reports():
    return jsonify(diagnostics.reports())


@app.route("/api/diagnostics/reports/<int:report_id>")
def api_diagnostic_report(report_id):
    report = diagnostics.report(report_id)
    if report is None:
        return jsonify({"success": False, "message": "Report not found"}), 404
    return jsonify(report)


@app.route("/api/charging")
def api_charging():
    return jsonify(trip_logger.get_charging_sessions())


@app.route("/api/trips")
def api_trips():
    return jsonify(trip_logger.get_all_trips())


@app.route("/api/trips/start", methods=["POST"])
def api_trips_start():
    with data_lock:
        snapshot = current_data.to_dict()
        soc = snapshot["soc_display"] if snapshot["soc_display"] is not None else snapshot["soc_bms"]
        if snapshot["is_charging"] is None or snapshot["speed_kmh"] is None or (snapshot["is_charging"] and snapshot["speed_kmh"] <= 2):
            return jsonify({"success": False, "message": "Fresh non-charging telemetry required"}), 409
    try:
        trip_id = trip_logger.start_trip(current_soc=soc)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 409
    return jsonify({"success": True, "trip_id": trip_id})


@app.route("/api/trips/stop", methods=["POST"])
def api_trips_stop():
    trip_id = trip_logger.stop_trip()
    return jsonify({"success": True, "finished_trip_id": trip_id})


@app.route("/api/trips/<int:trip_id>/csv")
def api_trips_csv(trip_id: int):
    csv_data = trip_logger.export_trip_csv(trip_id)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=ioniq_trip_{trip_id}.csv"}
    )


@app.route("/api/lcd/message", methods=["POST"])
def api_lcd_message():
    data = request.json or {}
    l1 = data.get("line1", "")
    l2 = data.get("line2", "")
    if not l1 and not l2 and "message" in data:
        msg = data.get("message", "")
        lines = msg.split("\\n") if "\\n" in msg else msg.split("\n")
        l1 = lines[0] if len(lines) > 0 else ""
        l2 = lines[1] if len(lines) > 1 else ""
    duration = float(data.get("duration", 5.0))
    lcd.show_message(l1, l2, duration=duration, force=True)
    return jsonify({"success": True, "message": f"Displayed on LCD: {l1} / {l2}"})


@app.route("/api/lcd/clear", methods=["POST"])
def api_lcd_clear():
    lcd.clear()
    return jsonify({"success": True})


# -------------------------------------------------------------
# Main Runner
# -------------------------------------------------------------

def start_threads():
    t_obd = threading.Thread(target=obd_worker, daemon=True, name="OBDWorker")
    t_lcd = threading.Thread(target=lcd_worker, daemon=True, name="LCDWorker")
    t_obd.start()
    t_lcd.start()
    logger.info("Background OBD and LCD workers started.")


if __name__ == "__main__":
    start_threads()
    port = int(config.get("web_port", 5000))
    logger.info("Starting web server on http://0.0.0.0:%d ...", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
