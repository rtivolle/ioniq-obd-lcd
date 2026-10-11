"""Main entry point and orchestrator for Hyundai Ioniq OBD2 & 1602A LCD System.
Runs Bluetooth manager, OBD2/Simulator polling loop, LCD display loop, trip logger,
and Flask web service.
"""

import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, render_template, request

# Import local modules
from bluetooth_manager import BluetoothManager, BluetoothSerialConnection
from config import load_config, save_config
from ioniq_obd import IoniqData, IoniqOBD, SimulatorIoniqOBD
from lcd_driver import LCDDriver
from trip_logger import TripLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ioniq_main")

# State & objects
config = load_config()
lcd = LCDDriver()
trip_logger = TripLogger()
sim_obd = SimulatorIoniqOBD()

bt_conn: Optional = None
live_obd: Optional[IoniqOBD] = None
current_data: IoniqData = IoniqData()
data_lock = threading.Lock()
running = True

app = Flask(__name__, template_folder="/home/rtivolle/ioniq_obd/templates")


# -------------------------------------------------------------
# Background Workers
# -------------------------------------------------------------

def obd_worker():
    """Background worker to query OBD-II adapter or run simulator."""
    global current_data, bt_conn, live_obd, running

    sim_obd.initialize_elm327()

    while running:
        sim_mode = config.get("simulation_mode", True)
        if sim_mode:
            try:
                sim_data = sim_obd.query_all()
                with data_lock:
                    current_data = sim_data
                trip_logger.record_sample(sim_data.to_dict(), auto_manage=config.get("trip_auto_record", True))
            except Exception as e:
                logger.error("Simulation error: %s", e)
            time.sleep(0.5)
            continue

        # Live OBD2 Mode
        mac = config.get("bluetooth_mac", "").strip()
        if not mac:
            with data_lock:
                current_data = IoniqData(connected=False, status_text="No MAC set")
            time.sleep(2.0)
            continue

        if bt_conn is None or not bt_conn.connected:
            if not config.get("auto_connect", True):
                with data_lock:
                    current_data = IoniqData(connected=False, status_text="Auto-connect off")
                time.sleep(2.0)
                continue

            with data_lock:
                current_data = IoniqData(connected=False, status_text="Connecting BT...")
            logger.info("Attempting auto-connect to %s...", mac)

            try:
                conn = BluetoothSerialConnection(mac=mac, channel=1, timeout=3.0)
                if conn.connect():
                    bt_conn = conn
                    live_obd = IoniqOBD(bt_conn)
                    if not live_obd.initialize_elm327():
                        logger.warning("ELM327 initialization failed, closing connection.")
                        bt_conn.close()
                        bt_conn = None
                        live_obd = None
                else:
                    bt_conn = None
                    live_obd = None
                    time.sleep(3.0)
                    continue
            except Exception as e:
                logger.error("BT connect error: %s", e)
                bt_conn = None
                live_obd = None
                time.sleep(3.0)
                continue

        # Connected to ELM327 - poll data
        try:
            polled = live_obd.query_all()
            with data_lock:
                current_data = polled
            if polled.connected:
                trip_logger.record_sample(polled.to_dict(), auto_manage=config.get("trip_auto_record", True))
            else:
                # Connection dropped
                logger.warning("OBD reported disconnected, resetting BT connection.")
                if bt_conn:
                    bt_conn.close()
                bt_conn = None
                live_obd = None
                time.sleep(2.0)
        except Exception as e:
            logger.error("OBD poll error: %s", e)
            if bt_conn:
                bt_conn.close()
            bt_conn = None
            live_obd = None
            time.sleep(2.0)

        time.sleep(0.3)


def lcd_worker():
    """Background worker to continuously update the 1602A LCD screen."""
    global running
    while running:
        try:
            with data_lock:
                obd_dict = current_data.to_dict()
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
        obd_dict = current_data.to_dict()
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
        config.update(new_data)
        save_config(config)
        return jsonify({"success": True, "config": config})
    return jsonify(config)


@app.route("/api/bluetooth/scan")
def api_bluetooth_scan():
    devices = BluetoothManager.scan_devices(timeout=5)
    return jsonify(devices)


@app.route("/api/bluetooth/connect", methods=["POST"])
def api_bluetooth_connect():
    global bt_conn, live_obd
    data = request.json or {}
    mac = data.get("mac", "").strip().upper()
    name = data.get("name", "").strip()
    pin = data.get("pin", "1234").strip()

    if not mac:
        return jsonify({"success": False, "message": "No MAC specified"}), 400

    # Disconnect any existing live connection
    if bt_conn:
        try:
            bt_conn.close()
        except Exception:
            pass
        bt_conn = None
        live_obd = None

    # Pair & trust
    pair_result = BluetoothManager.pair_and_trust(mac=mac, pin=pin)

    # Save to config to remember
    config["bluetooth_mac"] = mac
    config["bluetooth_name"] = name or mac
    config["bluetooth_pin"] = pin
    config["auto_connect"] = True
    config["simulation_mode"] = False
    save_config(config)

    # Attempt RFCOMM connection
    conn = BluetoothSerialConnection(mac=mac, channel=1, timeout=4.0)
    if conn.connect():
        bt_conn = conn
        live_obd = IoniqOBD(bt_conn)
        live_obd.initialize_elm327()
        return jsonify({"success": True, "message": f"Connected and remembered {name or mac}."})
    else:
        return jsonify({
            "success": False,
            "message": f"Pairing returned: {pair_result.get('message')}, but RFCOMM connection failed. Ensure OBD2 adapter is powered and vehicle is ON."
        })


@app.route("/api/bluetooth/reconnect", methods=["POST"])
def api_bluetooth_reconnect():
    global bt_conn, live_obd
    mac = config.get("bluetooth_mac", "").strip()
    if not mac:
        return jsonify({"success": False, "message": "No device remembered"}), 400

    if bt_conn:
        try:
            bt_conn.close()
        except Exception:
            pass
        bt_conn = None
        live_obd = None

    conn = BluetoothSerialConnection(mac=mac, channel=1, timeout=4.0)
    if conn.connect():
        bt_conn = conn
        live_obd = IoniqOBD(bt_conn)
        live_obd.initialize_elm327()
        return jsonify({"success": True, "message": f"Reconnected to {mac}."})
    return jsonify({"success": False, "message": f"Could not connect to {mac}."})


@app.route("/api/bluetooth/forget", methods=["POST"])
def api_bluetooth_forget():
    global bt_conn, live_obd
    mac = config.get("bluetooth_mac", "")
    if bt_conn:
        try:
            bt_conn.close()
        except Exception:
            pass
        bt_conn = None
        live_obd = None

    if mac:
        BluetoothManager.remove_device(mac)

    config["bluetooth_mac"] = ""
    config["bluetooth_name"] = ""
    save_config(config)
    return jsonify({"success": True, "message": "Remembered device cleared"})


@app.route("/api/trips")
def api_trips():
    return jsonify(trip_logger.get_all_trips())


@app.route("/api/trips/start", methods=["POST"])
def api_trips_start():
    with data_lock:
        soc = current_data.soc_display or current_data.soc_bms or 0.0
    trip_id = trip_logger.start_trip(current_soc=soc)
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
    msg = data.get("message", "")
    duration = float(data.get("duration", 5.0))
    lines = msg.split("\\n") if "\\n" in msg else msg.split("\n")
    l1 = lines[0] if len(lines) > 0 else ""
    l2 = lines[1] if len(lines) > 1 else ""
    lcd.show_message(l1, l2, duration=duration)
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
