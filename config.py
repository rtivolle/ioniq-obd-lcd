"""Configuration persistence for Ioniq OBD application."""

import json
import logging
import os
from typing import Any, Dict

CONFIG_FILE = "/home/rtivolle/ioniq_obd/config.json"

DEFAULT_CONFIG = {
    "bluetooth_mac": "",
    "bluetooth_name": "",
    "bluetooth_pin": "1234",
    "auto_connect": True,
    "simulation_mode": True,  # Default to simulation until user connects their adapter
    "lcd_auto_cycle": True,
    "lcd_cycle_seconds": 4,
    "lcd_line1": "soc",
    "lcd_line2": "power",
    "trip_auto_record": True,
    "web_port": 5000,
    "vehicle_model": "Hyundai Ioniq 2017"
}

logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(data)
                return config
        except Exception as e:
            logger.error("Failed to read %s: %s", CONFIG_FILE, e)
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> bool:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logger.error("Failed to write %s: %s", CONFIG_FILE, e)
        return False
