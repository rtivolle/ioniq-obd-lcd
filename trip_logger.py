"""Trip logger and statistics calculator for Hyundai Ioniq.
Stores trips and periodic telemetry samples in SQLite, calculates energy usage & efficiency,
and exports trips to CSV.
"""

import csv
import io
import logging
import os
import sqlite3
import time
import threading
from functools import wraps
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class TripLogger:
    def __init__(self, db_path: str = "/home/rtivolle/ioniq_obd/trips.db"):
        self.lock = threading.RLock()
        self.db_path = db_path
        self._init_db()
        self.active_trip_id: Optional[int] = None
        self.active_start_time: float = 0.0
        self.last_sample_time: float = 0.0
        self.distance_km: float = 0.0
        self.energy_used_kwh: float = 0.0
        self.energy_regen_kwh: float = 0.0
        self.max_speed_kmh: float = 0.0
        self.start_soc: float = 0.0
        self.last_soc: float = 0.0
        self.last_movement_time: float = 0.0
        self.state = "parked"
        self.active_charge_id = None
        self.charge_start_time = 0.0
        self.charge_start_soc = None
        self.charge_last_soc = None
        self.charge_energy = 0.0
        self.charge_duration = 0.0
        self.charge_last_sample = 0.0

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                distance_km REAL DEFAULT 0,
                energy_used_kwh REAL DEFAULT 0,
                energy_regen_kwh REAL DEFAULT 0,
                net_kwh REAL DEFAULT 0,
                max_speed_kmh REAL DEFAULT 0,
                avg_speed_kmh REAL DEFAULT 0,
                start_soc REAL DEFAULT 0,
                end_soc REAL DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trip_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER,
                timestamp TIMESTAMP,
                speed_kmh REAL,
                power_kw REAL,
                voltage REAL,
                current REAL,
                soc REAL,
                aux_voltage REAL,
                battery_temp INTEGER,
                FOREIGN KEY (trip_id) REFERENCES trips (id)
            )
        """)
        # Additive migration for existing installations.
        columns = {row[1] for row in cur.execute("PRAGMA table_info(trips)")}
        if "checkpoint_time" not in columns:
            cur.execute("ALTER TABLE trips ADD COLUMN checkpoint_time REAL")
        cur.execute("""CREATE TABLE IF NOT EXISTS charging_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT, end_time TEXT, checkpoint_time REAL,
            energy_added_kwh REAL DEFAULT 0, duration_sec REAL DEFAULT 0,
            start_soc REAL, end_soc REAL, soc_change REAL,
            status TEXT DEFAULT 'active'
        )""")
        # Preserve the last durable totals; never integrate across downtime.
        for table in ("trips", "charging_sessions"):
            cur.execute(f"""UPDATE {table} SET status = 'interrupted',
                end_time = COALESCE(datetime(checkpoint_time, 'unixepoch', 'localtime'), start_time)
                WHERE status = 'active'""")
        conn.commit()
        conn.close()

    @synchronized
    def start_trip(self, current_soc: float = 0.0) -> int:
        """Starts a new trip recording session."""
        if self.state == "charging":
            raise ValueError("Cannot start a driving trip while charging")
        if self.active_trip_id is not None:
            return self.active_trip_id
        now = time.time()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trips (start_time, start_soc, status)
            VALUES (datetime(?, 'unixepoch', 'localtime'), ?, 'active')
        """, (now, current_soc))
        trip_id = cur.lastrowid
        conn.commit()
        conn.close()

        self.active_trip_id = trip_id
        self.active_start_time = now
        self.last_sample_time = now
        self.last_movement_time = now
        self.distance_km = 0.0
        self.energy_used_kwh = 0.0
        self.energy_regen_kwh = 0.0
        self.max_speed_kmh = 0.0
        self.start_soc = current_soc
        self.last_soc = current_soc
        logger.info("Started trip ID %d", trip_id)
        return trip_id

    @synchronized
    def record_sample(self, obd_dict: Dict[str, Any], auto_manage: bool = True):
        """Integrate only consecutive valid samples, checkpointing each transaction."""
        now = time.time()
        keys = ("speed_kmh", "battery_power_kw", "is_charging")
        fresh = obd_dict.get("fresh", {})
        if not obd_dict.get("connected") or any(
            obd_dict.get(k) is None or not fresh.get(k, False) for k in keys
        ):
            self.last_sample_time = 0.0
            self.charge_last_sample = 0.0
            self.state = "unavailable"
            return
        speed = obd_dict["speed_kmh"]
        power = obd_dict["battery_power_kw"]
        soc = obd_dict.get("soc_display")
        if soc is None:
            soc = obd_dict.get("soc_bms")
        # Motion takes priority: negative power while moving is regenerative braking.
        self.state = "driving" if speed > 2 else ("charging" if obd_dict["is_charging"] else "parked")
        if self.state == "charging":
            self.stop_trip()
            if self.active_charge_id is None:
                if not auto_manage:
                    return
                with self._transaction() as conn:
                    self.active_charge_id = conn.execute("""INSERT INTO charging_sessions
                        (start_time, start_soc, end_soc, checkpoint_time)
                        VALUES (datetime(?, 'unixepoch', 'localtime'), ?, ?, ?)""",
                        (now, soc, soc, now)).lastrowid
                self.charge_start_time = now
                self.charge_start_soc = soc
                self.charge_last_soc = soc
                self.charge_energy = self.charge_duration = 0.0
                self.charge_last_sample = 0.0
            dt = now - self.charge_last_sample if self.charge_last_sample else 0
            if 0 < dt < 60:
                self.charge_energy += max(0, -power) * dt / 3600
                self.charge_duration += dt
            self.charge_last_sample = now
            if soc is not None:
                self.charge_last_soc = soc
            self._checkpoint_charge(now)
            return
        self._stop_charge(now)
        if auto_manage and self.active_trip_id is None and self.state == "driving":
            self.start_trip(soc)
        if self.active_trip_id is None:
            return
        dt = now - self.last_sample_time if self.last_sample_time else 0
        if 0 < dt < 60:
            self.distance_km += speed * dt / 3600
            self.energy_used_kwh += max(0, power) * dt / 3600
            self.energy_regen_kwh += max(0, -power) * dt / 3600
        self.max_speed_kmh = max(self.max_speed_kmh, speed)
        self.last_sample_time = now
        if soc is not None:
            self.last_soc = soc
        if self.state == "driving":
            self.last_movement_time = now
        with self._transaction() as conn:
            conn.execute("""INSERT INTO trip_samples
                (trip_id, timestamp, speed_kmh, power_kw, voltage, current, soc, aux_voltage, battery_temp)
                VALUES (?, datetime(?, 'unixepoch', 'localtime'), ?, ?, ?, ?, ?, ?, ?)""",
                (self.active_trip_id, now, speed, power, obd_dict.get("battery_voltage"),
                 obd_dict.get("battery_current"), soc, obd_dict.get("aux_voltage"),
                 obd_dict.get("battery_max_temp")))
            self._checkpoint_trip(conn, now)
        if auto_manage and now - self.last_movement_time > 600:
            self.stop_trip()

    def _checkpoint_trip(self, conn, now):
        duration = max(0.001, (now - self.active_start_time) / 3600)
        conn.execute("""UPDATE trips SET distance_km=?, energy_used_kwh=?,
            energy_regen_kwh=?, net_kwh=?, max_speed_kmh=?, avg_speed_kmh=?,
            end_soc=?, checkpoint_time=? WHERE id=?""",
            (self.distance_km, self.energy_used_kwh, self.energy_regen_kwh,
             self.energy_used_kwh - self.energy_regen_kwh, self.max_speed_kmh,
             self.distance_km / duration, self.last_soc, now, self.active_trip_id))

    def _checkpoint_charge(self, now, status="active"):
        change = (self.charge_last_soc - self.charge_start_soc
                  if self.charge_last_soc is not None and self.charge_start_soc is not None else None)
        with self._transaction() as conn:
            conn.execute("""UPDATE charging_sessions SET energy_added_kwh=?,
                duration_sec=?, end_soc=?, soc_change=?, checkpoint_time=?, status=?,
                end_time=CASE WHEN ?='completed' THEN datetime(?, 'unixepoch', 'localtime') ELSE NULL END
                WHERE id=?""", (self.charge_energy, self.charge_duration,
                self.charge_last_soc, change, now, status, status, now, self.active_charge_id))

    def _stop_charge(self, now):
        if self.active_charge_id is not None:
            self._checkpoint_charge(now, "completed")
            self.active_charge_id = None
            self.charge_last_sample = 0.0

    @synchronized
    def get_charging_sessions(self):
        with self._transaction() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM charging_sessions ORDER BY id DESC LIMIT 50")]

    @synchronized
    def stop_trip(self) -> Optional[int]:
        """Stops the active trip and writes summary metrics."""
        if self.active_trip_id is None:
            return None

        now = time.time()
        duration_hours = max(0.001, (now - self.active_start_time) / 3600.0)
        avg_speed = round(self.distance_km / duration_hours, 1)
        net_kwh = round(self.energy_used_kwh - self.energy_regen_kwh, 2)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            UPDATE trips
            SET end_time = datetime(?, 'unixepoch', 'localtime'),
                distance_km = ?,
                energy_used_kwh = ?,
                energy_regen_kwh = ?,
                net_kwh = ?,
                max_speed_kmh = ?,
                avg_speed_kmh = ?,
                end_soc = ?,
                status = 'completed'
            WHERE id = ?
        """, (
            now,
            round(self.distance_km, 2),
            round(self.energy_used_kwh, 2),
            round(self.energy_regen_kwh, 2),
            net_kwh,
            round(self.max_speed_kmh, 1),
            avg_speed,
            round(self.last_soc, 1) if self.last_soc is not None else None,
            self.active_trip_id
        ))
        conn.commit()
        conn.close()

        finished_id = self.active_trip_id
        self.active_trip_id = None
        logger.info("Finished trip ID %d (Distance: %.2f km, Net: %.2f kWh)", finished_id, self.distance_km, net_kwh)
        return finished_id

    @synchronized
    def get_active_trip_summary(self) -> Dict[str, Any]:
        """Returns live metrics for current active trip."""
        now = time.time()
        duration_sec = int(now - self.active_start_time) if self.active_trip_id else 0
        net_kwh = self.energy_used_kwh - self.energy_regen_kwh
        efficiency_kwh_100km = (net_kwh / self.distance_km * 100.0) if self.distance_km >= 0.1 else 0.0

        return {
            "state": self.state,
            "charging_id": self.active_charge_id,
            "active": self.active_trip_id is not None,
            "trip_id": self.active_trip_id,
            "duration_sec": duration_sec,
            "distance_km": round(self.distance_km, 2),
            "energy_used_kwh": round(self.energy_used_kwh, 2),
            "energy_regen_kwh": round(self.energy_regen_kwh, 2),
            "net_kwh": round(net_kwh, 2),
            "max_speed_kmh": round(self.max_speed_kmh, 1),
            "efficiency_kwh_100km": round(efficiency_kwh_100km, 1),
            "start_soc": round(self.start_soc, 1) if self.start_soc is not None else None,
            "current_soc": round(self.last_soc, 1) if self.last_soc is not None else None
        }

    @synchronized
    def get_all_trips(self) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trips ORDER BY id DESC LIMIT 50")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def export_trip_csv(self, trip_id: int) -> str:
        """Exports all samples of a given trip to CSV string."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, speed_kmh, power_kw, voltage, current, soc, aux_voltage, battery_temp
            FROM trip_samples
            WHERE trip_id = ?
            ORDER BY id ASC
        """, (trip_id,))
        rows = cur.fetchall()
        conn.close()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Timestamp", "Speed_kmh", "Power_kW", "Voltage_V", "Current_A", "SOC_pct", "Aux_V", "Battery_Temp_C"])
        for r in rows:
            writer.writerow(r)
        return output.getvalue()
