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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TripLogger:
    def __init__(self, db_path: str = "/home/rtivolle/ioniq_obd/trips.db"):
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
        conn.commit()
        conn.close()

    def start_trip(self, current_soc: float = 0.0) -> int:
        """Starts a new trip recording session."""
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

    def record_sample(self, obd_dict: Dict[str, Any], auto_manage: bool = True):
        """Processes a new telemetry sample from OBD."""
        speed = obd_dict.get("speed_kmh", 0.0)
        power = obd_dict.get("battery_power_kw", 0.0)
        soc = obd_dict.get("soc_display", obd_dict.get("soc_bms", 0.0))
        now = time.time()

        # Auto-start trip if moving and no trip active
        if auto_manage and self.active_trip_id is None:
            if speed > 4.0 or abs(power) > 2.0:
                self.start_trip(current_soc=soc)

        if self.active_trip_id is None:
            return

        # Time delta
        dt = now - self.last_sample_time if self.last_sample_time > 0 else 0.0
        if dt > 0 and dt < 60:
            # Distance integration
            dist_delta = (speed * (dt / 3600.0))
            self.distance_km += dist_delta

            # Energy integration (kW * hours)
            energy_delta_kwh = power * (dt / 3600.0)
            if energy_delta_kwh > 0:
                self.energy_used_kwh += energy_delta_kwh
            else:
                self.energy_regen_kwh += abs(energy_delta_kwh)

            if speed > self.max_speed_kmh:
                self.max_speed_kmh = speed

        self.last_sample_time = now
        self.last_soc = soc

        if speed > 2.0:
            self.last_movement_time = now

        # Periodic sample record into SQLite (at most once every 2 seconds)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trip_samples (trip_id, timestamp, speed_kmh, power_kw, voltage, current, soc, aux_voltage, battery_temp)
            VALUES (?, datetime(?, 'unixepoch', 'localtime'), ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.active_trip_id,
            now,
            speed,
            power,
            obd_dict.get("battery_voltage", 0.0),
            obd_dict.get("battery_current", 0.0),
            soc,
            obd_dict.get("aux_voltage", 0.0),
            obd_dict.get("battery_max_temp", 0)
        ))
        conn.commit()
        conn.close()

        # Auto-finish if stopped for more than 10 minutes
        if auto_manage and (now - self.last_movement_time > 600) and self.distance_km > 0.5:
            self.stop_trip()

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
            round(self.last_soc, 1),
            self.active_trip_id
        ))
        conn.commit()
        conn.close()

        finished_id = self.active_trip_id
        self.active_trip_id = None
        logger.info("Finished trip ID %d (Distance: %.2f km, Net: %.2f kWh)", finished_id, self.distance_km, net_kwh)
        return finished_id

    def get_active_trip_summary(self) -> Dict[str, Any]:
        """Returns live metrics for current active trip."""
        now = time.time()
        duration_sec = int(now - self.active_start_time) if self.active_trip_id else 0
        net_kwh = self.energy_used_kwh - self.energy_regen_kwh
        efficiency_kwh_100km = (net_kwh / self.distance_km * 100.0) if self.distance_km >= 0.1 else 0.0

        return {
            "active": self.active_trip_id is not None,
            "trip_id": self.active_trip_id,
            "duration_sec": duration_sec,
            "distance_km": round(self.distance_km, 2),
            "energy_used_kwh": round(self.energy_used_kwh, 2),
            "energy_regen_kwh": round(self.energy_regen_kwh, 2),
            "net_kwh": round(net_kwh, 2),
            "max_speed_kmh": round(self.max_speed_kmh, 1),
            "efficiency_kwh_100km": round(efficiency_kwh_100km, 1),
            "start_soc": round(self.start_soc, 1),
            "current_soc": round(self.last_soc, 1)
        }

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
