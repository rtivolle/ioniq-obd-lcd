"""OBD-II communication and parsing for Hyundai Ioniq 2017 (EV / HEV / PHEV).
Handles ELM327 protocol, extended BMS CAN commands (Header 7E4), Mode 01 PIDs,
and realistic simulator fallback.
"""

import logging
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class IoniqData:
    connected: bool = False
    timestamp: float = 0.0
    speed_kmh: float = 0.0
    motor_rpm: int = 0
    soc_bms: float = 0.0
    soc_display: float = 0.0
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    battery_power_kw: float = 0.0
    battery_max_temp: int = 0
    battery_min_temp: int = 0
    aux_voltage: float = 0.0
    soh: float = 100.0
    max_charge_power_kw: float = 0.0
    max_discharge_power_kw: float = 0.0
    cum_energy_charged_kwh: float = 0.0
    cum_energy_discharged_kwh: float = 0.0
    is_charging: bool = False
    status_text: str = "Disconnected"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IoniqOBD:
    """Manages ELM327 connection and queries for Hyundai Ioniq."""

    def __init__(self, connection):
        self.conn = connection
        self.data = IoniqData()
        self.last_query_time = 0.0
        self._current_header = ""

    def initialize_elm327(self) -> bool:
        """Initializes ELM327 with recommended settings for Hyundai CAN bus."""
        logger.info("Initializing ELM327 adapter...")
        init_cmds = [
            ("AT Z", 2.0),     # Reset
            ("AT E0", 0.5),    # Echo off
            ("AT L0", 0.5),    # Linefeeds off
            ("AT S0", 0.5),    # Spaces off
            ("AT SP 6", 0.5),   # ISO 15765-4 CAN (11 bit ID, 500 kbaud)
            ("AT CAF 1", 0.5),  # CAN Auto Formatting on
            ("AT AT 1", 0.5),   # Adaptive timing on
        ]
        for cmd, delay in init_cmds:
            resp = self._send_command(cmd, timeout=delay)
            logger.debug("Init [%s] -> %r", cmd, resp)
            time.sleep(0.05)

        self.data.connected = True
        self.data.status_text = "ELM327 Initialized"
        return True

    def _send_command(self, cmd: str, timeout: float = 1.5) -> str:
        """Sends command and waits for prompt '>'."""
        if not self.conn or not self.conn.connected:
            return ""
        try:
            self.conn.flush_input()
            self.conn.write((cmd + "\r").encode("ascii"))
            resp = self.conn.read_until(b">", timeout=timeout)
            text = resp.decode("ascii", errors="ignore").replace(">", "").strip()
            return text
        except Exception as e:
            logger.error("Error sending %s: %s", cmd, e)
            self.data.connected = False
            self.data.status_text = f"Comm error: {e}"
            return ""

    def set_header(self, header: str):
        """Sets CAN header if different from current."""
        if self._current_header != header:
            self._send_command(f"AT SH {header}", timeout=0.5)
            self._current_header = header

    def _parse_hex_payload(self, raw_resp: str, expected_prefix: str) -> Optional[bytes]:
        """Strips CAN multi-frame numbering and returns raw byte payload."""
        if not raw_resp or "NO DATA" in raw_resp or "ERROR" in raw_resp:
            return None

        clean_lines = []
        for line in raw_resp.splitlines():
            line = line.strip().replace(" ", "")
            # Filter out lines starting with frame numbers like '0:', '1:', etc.
            line = re.sub(r"^[0-9A-Fa-f]:", "", line)
            # Remove ECU header if present (e.g. 7EC)
            if line.startswith("7EC") or line.startswith("7EA"):
                line = line[3:]
            clean_lines.append(line)

        hex_text = "".join(clean_lines)
        if expected_prefix in hex_text:
            idx = hex_text.find(expected_prefix)
            hex_payload = hex_text[idx + len(expected_prefix):]
            # Ensure even length
            if len(hex_payload) % 2 != 0:
                hex_payload = hex_payload[:-1]
            try:
                return bytes.fromhex(hex_payload)
            except ValueError:
                return None
        return None

    def query_all(self) -> IoniqData:
        """Performs a full poll cycle of BMS extended PIDs and vehicle speed."""
        now = time.time()
        self.data.timestamp = now

        try:
            # 1. Query Speed (Standard OBD2 010D)
            self.set_header("7DF")  # Broadcast / standard OBD
            speed_resp = self._send_command("01 0D", timeout=0.8)
            speed_bytes = self._parse_hex_payload(speed_resp, "410D")
            if speed_bytes and len(speed_bytes) >= 1:
                self.data.speed_kmh = float(speed_bytes[0])

            # 2. Query BMS Extended PID 2101 (Header 7E4)
            self.set_header("7E4")
            bms_resp = self._send_command("21 01", timeout=1.8)
            bms_bytes = self._parse_hex_payload(bms_resp, "6101")
            if bms_bytes and len(bms_bytes) >= 30:
                # Byte 4 (E): SOC BMS (%)
                self.data.soc_bms = round(bms_bytes[4] / 2.0, 1)

                # Bytes 5, 6: Max charge power (kW)
                self.data.max_charge_power_kw = round(((bms_bytes[5] << 8) | bms_bytes[6]) / 100.0, 1)

                # Bytes 7, 8: Max discharge power (kW)
                self.data.max_discharge_power_kw = round(((bms_bytes[7] << 8) | bms_bytes[8]) / 100.0, 1)

                # Flag byte 9: charging indicators
                flag_j = bms_bytes[9]
                self.data.is_charging = bool(flag_j & (1 << 7))

                # Bytes 10, 11 (K, L): Battery Current (A) - signed 16-bit
                raw_curr = (bms_bytes[10] << 8) | bms_bytes[11]
                if raw_curr >= 32768:
                    raw_curr -= 65536
                self.data.battery_current = round(raw_curr / 10.0, 1)

                # Bytes 12, 13 (M, N): Battery Voltage (V)
                raw_volt = (bms_bytes[12] << 8) | bms_bytes[13]
                self.data.battery_voltage = round(raw_volt / 10.0, 1)

                # Calculate instantaneous power: kW = V * A / 1000
                self.data.battery_power_kw = round((self.data.battery_voltage * self.data.battery_current) / 1000.0, 2)

                # Byte 14, 15: Battery max and min temperatures
                max_t = bms_bytes[14]
                min_t = bms_bytes[15]
                self.data.battery_max_temp = max_t if max_t < 128 else max_t - 256
                self.data.battery_min_temp = min_t if min_t < 128 else min_t - 256

                # Byte 29: Aux 12V Battery Voltage
                if len(bms_bytes) > 29:
                    self.data.aux_voltage = round(bms_bytes[29] * 0.1, 1)

                # Cumulative energies
                if len(bms_bytes) >= 44:
                    cec = (bms_bytes[36] << 24) | (bms_bytes[37] << 16) | (bms_bytes[38] << 8) | bms_bytes[39]
                    ced = (bms_bytes[40] << 24) | (bms_bytes[41] << 16) | (bms_bytes[42] << 8) | bms_bytes[43]
                    self.data.cum_energy_charged_kwh = round(cec / 10.0, 1)
                    self.data.cum_energy_discharged_kwh = round(ced / 10.0, 1)

                # Motor RPM (bytes 53, 54 if present)
                if len(bms_bytes) >= 55:
                    rpm = (bms_bytes[53] << 8) | bms_bytes[54]
                    if rpm >= 32768:
                        rpm -= 65536
                    self.data.motor_rpm = rpm

            # 3. Query BMS Extended PID 2105 (SOH & Display SOC)
            soh_resp = self._send_command("21 05", timeout=1.5)
            soh_bytes = self._parse_hex_payload(soh_resp, "6105")
            if soh_bytes and len(soh_bytes) >= 32:
                # Bytes 25, 26: SOH (%)
                soh_raw = (soh_bytes[25] << 8) | soh_bytes[26]
                self.data.soh = round(soh_raw / 10.0, 1)

                # Byte 31: Display SOC (%)
                self.data.soc_display = round(soh_bytes[31] / 2.0, 1)
            elif self.data.soc_bms > 0 and self.data.soc_display == 0:
                self.data.soc_display = self.data.soc_bms

            self.data.connected = True
            self.data.status_text = "Live Data OK"
        except Exception as e:
            logger.error("Error during query_all: %s", e)
            self.data.connected = False
            self.data.status_text = f"Error: {e}"

        return self.data


class SimulatorIoniqOBD:
    """Simulates realistic Hyundai Ioniq driving data for testing without car/OBD adapter."""

    def __init__(self):
        self.data = IoniqData()
        self.data.connected = True
        self.data.soc_bms = 78.5
        self.data.soc_display = 78.0
        self.data.battery_voltage = 368.5
        self.data.aux_voltage = 14.1
        self.data.soh = 99.4
        self.data.max_charge_power_kw = 70.0
        self.data.max_discharge_power_kw = 88.0
        self.data.battery_max_temp = 24
        self.data.battery_min_temp = 22
        self.data.cum_energy_charged_kwh = 1420.5
        self.data.cum_energy_discharged_kwh = 1385.2
        self.data.status_text = "Simulation Active"
        self._step = 0

    def initialize_elm327(self) -> bool:
        self.data.connected = True
        self.data.status_text = "Simulation Ready"
        return True

    def query_all(self) -> IoniqData:
        self._step += 1
        t = self._step * 0.5
        now = time.time()
        self.data.timestamp = now

        # Simulate vehicle driving profile (accelerating, cruising, regenerating)
        cycle = (math.sin(t / 8.0) + math.sin(t / 3.0)) / 2.0
        speed = max(0.0, cycle * 55.0 + 35.0)
        self.data.speed_kmh = round(speed, 1)
        self.data.motor_rpm = int(speed * 68)

        # Acceleration vs regen
        accel = math.cos(t / 8.0)
        if accel > 0.1:
            # Consuming power
            power = accel * 35.0 + 3.0
            current = (power * 1000.0) / self.data.battery_voltage
        elif accel < -0.1:
            # Regenerative braking
            power = accel * 22.0
            current = (power * 1000.0) / self.data.battery_voltage
        else:
            power = 1.2
            current = (power * 1000.0) / self.data.battery_voltage

        self.data.battery_power_kw = round(power, 2)
        self.data.battery_current = round(current, 1)

        # Battery voltage responds to load
        base_v = 360.0 + (self.data.soc_bms / 100.0) * 20.0
        voltage_drop = current * 0.08
        self.data.battery_voltage = round(base_v - voltage_drop, 1)

        # Very slowly drain battery or regen
        self.data.soc_bms = max(5.0, min(100.0, self.data.soc_bms - (power * 0.00015)))
        self.data.soc_display = round(self.data.soc_bms, 1)

        # Minor aux voltage and temperature variations
        self.data.aux_voltage = round(13.9 + 0.3 * math.sin(t / 20.0), 1)
        self.data.battery_max_temp = 24 + int(abs(current) / 30.0)
        self.data.connected = True
        self.data.status_text = "Simulation OK"

        return self.data
