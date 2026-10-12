"""Bluetooth Manager for OBD-II ELM327 adapter.
Supports scanning, pairing with PIN, trusting, and native RFCOMM socket communication.
"""

import logging
import os
import re
import socket
import subprocess
import time
from typing import Dict, List, Optional

import pexpect

logger = logging.getLogger(__name__)


class BluetoothSerialConnection:
    """Wrapper around AF_BLUETOOTH RFCOMM socket or PySerial device."""

    def __init__(self, mac: str, channel: int = 1, timeout: float = 3.0):
        self.mac = mac
        self.channel = channel
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self._buffer = b""

    def connect(self) -> bool:
        self.close()
        channels = [self.channel] + [c for c in [2, 1, 3, 4] if c != self.channel]
        for ch in channels:
            try:
                logger.info("Connecting RFCOMM socket to %s on channel %s...", self.mac, ch)
                sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
                sock.settimeout(self.timeout)
                sock.connect((self.mac, ch))
                self.sock = sock
                self.channel = ch
                self.connected = True
                logger.info("Connected to %s on channel %s successfully.", self.mac, ch)
                return True
            except (ConnectionRefusedError, OSError) as e:
                logger.debug("Channel %s failed on %s: %s", ch, self.mac, e)
                try:
                    sock.close()
                except Exception:
                    pass
                continue
            except Exception as e:
                logger.error("Failed to connect to %s on channel %s: %s", self.mac, ch, e)
                self.close()
                return False
        return False

    def write(self, data: bytes) -> int:
        if not self.connected or not self.sock:
            raise ConnectionError("Not connected")
        return self.sock.send(data)

    def read_until(self, expected: bytes = b">", timeout: Optional[float] = None) -> bytes:
        """Read until expected delimiter or timeout."""
        if not self.connected or not self.sock:
            raise ConnectionError("Not connected")

        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        original_timeout = self.sock.gettimeout()

        try:
            while expected not in self._buffer:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.sock.settimeout(max(0.01, remaining))
                try:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        raise ConnectionError("Remote end closed connection")
                    self._buffer += chunk
                except socket.timeout:
                    break

            if expected in self._buffer:
                idx = self._buffer.index(expected) + len(expected)
                result = self._buffer[:idx]
                self._buffer = self._buffer[idx:]
                return result
            else:
                result = self._buffer
                self._buffer = b""
                return result
        finally:
            if self.sock:
                self.sock.settimeout(original_timeout)

    def flush_input(self):
        """Clear any buffered received data."""
        self._buffer = b""
        if self.sock and self.connected:
            self.sock.setblocking(False)
            try:
                while True:
                    data = self.sock.recv(1024)
                    if not data:
                        break
            except (BlockingIOError, socket.error):
                pass
            finally:
                if self.sock:
                    self.sock.settimeout(self.timeout)

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._buffer = b""


class BluetoothManager:
    """Manages discovery, pairing, and connection to Bluetooth devices via bluetoothctl."""

    @staticmethod
    def ensure_powered() -> bool:
        """Ensure bluetooth controller is unblocked and powered on."""
        try:
            subprocess.run(["rfkill", "unblock", "bluetooth"], check=False)
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, text=True, timeout=5)
            return True
        except Exception as e:
            logger.error("Error powering bluetooth: %s", e)
            return False

    @staticmethod
    def scan_devices(timeout: int = 5) -> List[Dict[str, str]]:
        """Scans for Bluetooth devices and returns list of dictionaries with info."""
        BluetoothManager.ensure_powered()
        devices: Dict[str, Dict[str, str]] = {}

        # 1. Fetch already known / paired devices
        try:
            proc = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5)
            for line in proc.stdout.splitlines():
                m = re.search(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", line)
                if m:
                    mac = m.group(1).upper()
                    name = m.group(2).strip()
                    devices[mac] = {
                        "mac": mac,
                        "name": name,
                        "paired": False,
                        "connected": False,
                        "rssi": "N/A"
                    }
        except Exception as e:
            logger.warning("Error fetching known devices: %s", e)

        # 2. Run active scan
        try:
            proc = subprocess.run(
                ["bluetoothctl", "--timeout", str(timeout), "scan", "on"],
                capture_output=True,
                text=True,
                timeout=timeout + 3
            )
            for line in proc.stdout.splitlines():
                m = re.search(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", line)
                if m:
                    mac = m.group(1).upper()
                    name = m.group(2).strip()
                    if mac not in devices:
                        devices[mac] = {
                            "mac": mac,
                            "name": name,
                            "paired": False,
                            "connected": False,
                            "rssi": "N/A"
                        }
                    else:
                        devices[mac]["name"] = name
        except Exception as e:
            logger.warning("Active scan completed or interrupted: %s", e)

        # 3. Augment details for each device
        result = []
        for mac, dev in devices.items():
            info = BluetoothManager.get_device_info(mac)
            if info:
                dev["name"] = info.get("name", dev["name"])
                dev["paired"] = info.get("paired", False)
                dev["connected"] = info.get("connected", False)
                dev["rssi"] = info.get("rssi", dev["rssi"])
            # Filter out devices where name is just the MAC address if preferred, but keep all for completeness
            result.append(dev)

        # Sort: OBD/Car looking names first, then paired, then name
        def sort_key(d):
            n = d["name"].lower()
            is_obd = 0 if any(k in n for k in ["obd", "elm", "vgate", "link", "car", "veepeak", "bafx"]) else 1
            is_paired = 0 if d["paired"] else 1
            return (is_obd, is_paired, d["name"])

        result.sort(key=sort_key)
        return result

    @staticmethod
    def get_device_info(mac: str) -> Optional[Dict]:
        """Queries info for a specific MAC address."""
        try:
            proc = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=4)
            if proc.returncode != 0 or "Missing device address" in proc.stdout:
                return None

            info = {
                "mac": mac,
                "name": mac,
                "paired": False,
                "trusted": False,
                "connected": False,
                "rssi": "N/A"
            }
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith("Name:"):
                    info["name"] = line.split("Name:", 1)[1].strip()
                elif line.startswith("Alias:"):
                    info["alias"] = line.split("Alias:", 1)[1].strip()
                elif line.startswith("Paired:"):
                    info["paired"] = "yes" in line.lower()
                elif line.startswith("Trusted:"):
                    info["trusted"] = "yes" in line.lower()
                elif line.startswith("Connected:"):
                    info["connected"] = "yes" in line.lower()
                elif line.startswith("RSSI:"):
                    info["rssi"] = line.split("RSSI:", 1)[1].strip()
            return info
        except Exception as e:
            logger.error("Failed to get info for %s: %s", mac, e)
            return None

    @staticmethod
    def pair_and_trust(mac: str, pin: str = "1234", timeout: int = 25) -> Dict:
        """Pairs and trusts a Bluetooth device using bluetoothctl and automated PIN entry."""
        BluetoothManager.ensure_powered()
        logger.info("Attempting to pair with %s using PIN %s...", mac, pin)

        try:
            child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=timeout)
            child.sendline("agent on")
            child.expect(r"agent registered|Agent is already registered", timeout=3)
            child.sendline("default-agent")
            child.expect(r"Default agent request successful|already default", timeout=3)

            child.sendline(f"pair {mac}")

            paired = False
            for _ in range(6):
                idx = child.expect([
                    r"Enter PIN code:",
                    r"Enter passkey:",
                    r"Confirm passkey",
                    r"Pairing successful",
                    r"Failed to pair",
                    r"AlreadyExists",
                    r"Device not available",
                    pexpect.TIMEOUT
                ], timeout=8)

                if idx in (0, 1):
                    logger.info("Sending PIN %s...", pin)
                    child.sendline(pin)
                elif idx == 2:
                    child.sendline("yes")
                elif idx in (3, 5):
                    paired = True
                    break
                elif idx in (4, 6):
                    err_msg = child.before.strip() if child.before else "Pairing rejected"
                    child.sendline("quit")
                    return {"success": False, "message": f"Pairing failed: {err_msg}"}
                elif idx == 7:  # Timeout
                    break

            # Trust the device
            child.sendline(f"trust {mac}")
            time.sleep(1)
            child.sendline("quit")
            child.close()

            # Double check pairing state
            info = BluetoothManager.get_device_info(mac)
            if info and (info.get("paired") or info.get("trusted")):
                return {"success": True, "message": f"Successfully paired and trusted {mac}"}

            return {"success": paired, "message": "Pairing finished (check status)"}
        except Exception as e:
            logger.error("Error in pair_and_trust for %s: %s", mac, e)
            return {"success": False, "message": str(e)}

    @staticmethod
    def remove_device(mac: str) -> bool:
        """Removes/unpairs a device."""
        try:
            proc = subprocess.run(["bluetoothctl", "remove", mac], capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception as e:
            logger.error("Error removing %s: %s", mac, e)
            return False
