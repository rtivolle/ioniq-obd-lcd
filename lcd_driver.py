"""Thread-safe LCD controller for 1602A display on Raspberry Pi GPIO.
Supports customizable lines, automatic multi-page cycling, and temporary message overlays.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LCDDriver:
    """Controls 1602A LCD via Adafruit CircuitPython CharacterLCD."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lcd = None
        self._init_lcd()
        self.custom_override_until: float = 0.0
        self.current_page_idx = 0
        self.last_page_switch = time.time()
        self.last_line1 = ""
        self.last_line2 = ""
        self.last_write_time = 0.0

    def _init_lcd(self):
        try:
            import board
            import digitalio
            import adafruit_character_lcd.character_lcd as characterlcd

            lcd_rs = digitalio.DigitalInOut(board.D22)
            lcd_en = digitalio.DigitalInOut(board.D17)
            lcd_d4 = digitalio.DigitalInOut(board.D25)
            lcd_d5 = digitalio.DigitalInOut(board.D24)
            lcd_d6 = digitalio.DigitalInOut(board.D23)
            lcd_d7 = digitalio.DigitalInOut(board.D18)

            self.lcd = characterlcd.Character_LCD_Mono(
                lcd_rs, lcd_en, lcd_d4, lcd_d5, lcd_d6, lcd_d7, 16, 2
            )
            self.lcd.clear()
            logger.info("1602A LCD initialized successfully.")
        except Exception as e:
            logger.error("Failed to initialize hardware LCD: %s", e)
            self.lcd = None

    def clear(self):
        with self.lock:
            if self.lcd:
                try:
                    self.lcd.clear()
                except Exception as e:
                    logger.warning("LCD clear error: %s", e)
            self.last_line1 = ""
            self.last_line2 = ""

    def show_message(self, line1: str, line2: str = "", duration: float = 0.0, force: bool = False):
        """Displays arbitrary text. If duration > 0, locks out background updates for that time."""
        with self.lock:
            now = time.time()
            l1 = (line1[:16]).ljust(16)
            l2 = (line2[:16]).ljust(16)
            if force or l1 != self.last_line1 or l2 != self.last_line2 or (now - self.last_write_time > 5.0):
                if self.lcd:
                    try:
                        self.lcd.clear()
                        self.lcd.message = f"{l1}\n{l2}"
                        self.last_write_time = now
                    except Exception as e:
                        logger.warning("LCD write error: %s", e)
                        try:
                            self._init_lcd()
                        except Exception:
                            pass
                self.last_line1 = l1
                self.last_line2 = l2
            if duration > 0:
                self.custom_override_until = now + duration

    def format_metric(self, key: str, obd_dict: Dict[str, Any], trip_dict: Dict[str, Any]) -> str:
        """Formats a single metric into a compact LCD-friendly string."""
        metrics = {"soc": "soc_display", "power": "battery_power_kw", "speed": "speed_kmh",
                   "voltage": "battery_voltage", "current": "battery_current", "aux": "aux_voltage",
                   "temp": "battery_max_temp", "soh": "soh", "rpm": "motor_rpm"}
        if key == "soc" and obd_dict.get("soc_display") is None:
            obd_dict = dict(obd_dict, soc_display=obd_dict.get("soc_bms"))
        if key in metrics and obd_dict.get(metrics[key]) is None:
            return f"{key[:3].title()}:--"
        if key == "soc":
            val = obd_dict.get("soc_display", obd_dict.get("soc_bms", 0.0))
            return f"S:{val:4.1f}%"
        elif key == "power":
            val = obd_dict.get("battery_power_kw", 0.0)
            return f"P:{val:+.1f}k"
        elif key == "speed":
            val = obd_dict.get("speed_kmh")
            val = float(val) if val is not None else 0.0
            return f"Spd:{int(val):3d}"
        elif key == "voltage":
            val = obd_dict.get("battery_voltage", 0.0)
            return f"HV:{int(round(val)):3d}V"
        elif key == "current":
            val = obd_dict.get("battery_current", 0.0)
            return f"I:{val:4.1f}A"
        elif key == "aux":
            val = obd_dict.get("aux_voltage", 0.0)
            return f"Aux:{val:4.1f}"
        elif key == "temp":
            val = obd_dict.get("battery_max_temp", 0)
            return f"T:{val:2d}C"
        elif key == "soh":
            val = obd_dict.get("soh", 100.0)
            return f"SOH:{int(round(val)):2d}%"
        elif key == "rpm":
            val = obd_dict.get("motor_rpm", 0)
            return f"RPM:{val:4d}"
        elif key == "trip_dist":
            dist = trip_dict.get("distance_km", 0.0)
            return f"D:{dist:4.1f}k"
        elif key == "trip_time":
            dur = int(trip_dict.get("duration_sec", 0))
            mins = dur // 60
            return f"T:{mins:3d}m"
        elif key == "trip_kwh":
            kwh = trip_dict.get("net_kwh", 0.0)
            return f"E:{kwh:4.1f}k"
        return key[:7]

    def update_from_data(
        self,
        obd_dict: Dict[str, Any],
        trip_dict: Dict[str, Any],
        config: Dict[str, Any]
    ):
        """Updates display based on current configuration and live metrics."""
        now = time.time()
        if now < self.custom_override_until:
            return  # Active custom overlay

        # Check if connected
        is_conn = obd_dict.get("connected", False)
        if not is_conn:
            status = obd_dict.get("status_text", "Disconnected")
            self.show_message("OBD2 Waiting...", status[:16])
            return

        if obd_dict.get("battery_voltage") is None and obd_dict.get("soc_bms") is None:
            self.show_message("Data unavailable", "Waiting for OBD")
            return

        auto_cycle = config.get("lcd_auto_cycle", True)
        cycle_sec = max(2, config.get("lcd_cycle_seconds", 4))

        if auto_cycle:
            # Multi-page rotating view
            metric = lambda key: self.format_metric(key, obd_dict, trip_dict)
            pages = [
                (f"{metric('soc')} {metric('power')}", f"{metric('speed')} {metric('voltage')}"),
                (f"{metric('aux')} {metric('temp')}", f"{metric('soh')} {metric('current')}"),
                (f"{metric('trip_dist')} {metric('trip_kwh')}", metric('trip_time')),
            ]

            if now - self.last_page_switch >= cycle_sec:
                self.current_page_idx = (self.current_page_idx + 1) % len(pages)
                self.last_page_switch = now

            line1, line2 = pages[self.current_page_idx]
            self.show_message(line1, line2)
        else:
            # Custom fixed lines
            m1 = config.get("lcd_line1", "soc")
            m2 = config.get("lcd_line2", "power")
            l1 = f"{self.format_metric(m1, obd_dict, trip_dict)} {self.format_metric('speed', obd_dict, trip_dict)}"
            l2 = f"{self.format_metric(m2, obd_dict, trip_dict)} {self.format_metric('aux', obd_dict, trip_dict)}"
            self.show_message(l1[:16], l2[:16])
