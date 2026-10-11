# Hyundai Ioniq 2017 OBD2 Telemetry & 1602A LCD Monitor

A lightweight, dedicated Raspberry Pi monitoring system for the **Hyundai Ioniq 2017 (EV / HEV / PHEV)**. Reads deep Battery Management System (BMS) telemetry through an ELM327 Bluetooth OBD-II adapter, displays live metrics on a 1602A character LCD mounted in your car, logs trips to SQLite with CSV export, and provides an offline-first responsive web dashboard.

---

## Features

- **Bluetooth OBD2 Auto-Connect**:
  - Scans nearby Bluetooth adapters via web UI.
  - Automatically handles PIN pairing (`1234` or `0000`) and trusts the device.
  - Persists MAC address and **automatically reconnects on boot** when the vehicle starts.
- **Hyundai Ioniq BMS Telemetry (CAN Header 7E4)**:
  - State of Charge (SOC Display & BMS SOC %)
  - Instantaneous Battery Power (kW) with regenerative braking detection
  - Battery DC Pack Voltage (V) & Amperage (A)
  - Battery Min/Max Module Temperatures (°C)
  - 12V Auxiliary Battery Voltage (V)
  - State of Health (SOH %)
  - Vehicle Speed (km/h) & Motor RPM
  - Cumulative energy charged/discharged (kWh)
- **1602A Character LCD Integration**:
  - Direct 4-bit GPIO driving (no potentiometer needed; V0 wired to GND).
  - Auto-rotates multiple pages (Driving stats, Battery health, Trip stats).
  - Supports custom user-selected metric pinning and custom message alerts.
- **Trip Logger & Efficiency**:
  - Automatically records trips when vehicle moves.
  - Computes net energy consumed, energy regenerated, and efficiency in **kWh / 100km**.
  - Historical trip browser and 1-click **CSV export** for data analysis.
- **Mobile-Responsive Web Dashboard**:
  - Dark mode web UI served on port `5000`.
  - Works completely offline in the car without internet connectivity.
  - Live virtual mirror of the physical LCD screen.

---

## 1602A LCD Hardware Wiring (Raspberry Pi 3B)

Direct 4-bit parallel GPIO connection. Potentiometer is skipped by tying **V0 (Pin 3)** directly to Ground:

| LCD Pin # | Pin Name | Wire To (Raspberry Pi) | Notes |
| :--- | :--- | :--- | :--- |
| **1** | VSS / GND | **GND** (Pin 6 or 9) | Ground |
| **2** | VDD / VCC | **5V** (Pin 2 or 4) | Power |
| **3** | V0 (Contrast) | **GND** (Pin 14 or 20) | Maximum contrast (no pot needed) |
| **4** | RS | **GPIO 22** (Pin 15) | Register Select |
| **5** | RW | **GND** (Pin 25) | Write mode |
| **6** | E (Enable) | **GPIO 17** (Pin 11) | Clock / Enable |
| **7-10**| D0 - D3 | *Not connected* | 4-bit mode |
| **11** | D4 | **GPIO 25** (Pin 22) | Data bit 4 |
| **12** | D5 | **GPIO 24** (Pin 18) | Data bit 5 |
| **13** | D6 | **GPIO 23** (Pin 16) | Data bit 6 |
| **14** | D7 | **GPIO 18** (Pin 12) | Data bit 7 |
| **15** | A (Backlight +)| **5V** (via 220Ω resistor or 3.3V) | Backlight Anode |
| **16** | K (Backlight -)| **GND** | Backlight Cathode |

---

## Software Installation

```bash
# Clone the repository
git clone https://github.com/rtivolle/ioniq-obd-lcd.git
cd ioniq-obd-lcd

# Install system dependencies
sudo apt-get update
sudo apt-get install -y python3-rpi-lgpio i2c-tools bluetooth bluez

# Set up Python virtual environment
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Running as a Systemd Service

To automatically start on boot:

```bash
sudo cp ioniq-obd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ioniq-obd
sudo systemctl start ioniq-obd
```

### Checking Service Status:
```bash
sudo systemctl status ioniq-obd
sudo journalctl -u ioniq-obd -f
```

---

## Usage

1. Open your browser on any phone, tablet, or laptop on your Tailnet or local Wi-Fi:
   - **Tailnet HTTPS URL**: `https://rspi3b-1.tail7355c5.ts.net/`
   - **Tailnet Direct IP**: `http://100.75.142.73:5000`
   - **Local Network**: `http://<raspberry-pi-ip>:5000` (or `http://rspi3b.local:5000`)
2. Navigate to the **"Bluetooth & OBD"** tab.
3. Click **"Scan for Bluetooth Adapters"**, find your ELM327 dongle, and click **"Pair & Connect"**.
4. The system will connect and begin streaming live vehicle CAN bus data and logging trips.

---

## License

MIT License.

## Data validity and session recovery

Each telemetry field has `fresh` and `metric_updated_at` metadata in
`/api/status`. Failed or missing payloads invalidate their fields immediately;
fields also expire after 10 seconds. Unavailable values are JSON `null`, rather
than zero or an old reading. Three consecutive polls without fresh speed,
battery power, and charging flags cause the worker to reconnect (when automatic
connection is enabled). Only the OBD worker owns Bluetooth I/O; web operations
are queued between polls, including scanning, pairing, reconnecting, forgetting,
and `/api/bluetooth/disconnect`.

Automatic driving trips start above 2 km/h. Stationary battery consumption does
not start a trip. Stationary charging indicated by the BMS ends the driving trip
and records a separate charging session. Motion takes priority over the charging
flag, so regenerative braking remains driving energy. `/api/charging` returns
charging history with battery energy added, recorded duration, and SOC change.
Energy added measures battery-side power, not electricity drawn at the wall.
Manual trip recording is refused during charging or unavailable telemetry.

Samples and trip totals commit together in SQLite on every valid poll. Charging
totals are also checkpointed every valid poll. On startup, unfinished sessions
are retained as `interrupted`, ending at their last checkpoint; the next session
starts separately. Missing-data intervals and time powered off are excluded from
energy and distance integration. Charging duration counts valid sample intervals.
Existing databases receive an additive migration automatically. Back up
`trips.db` before upgrading. Old sessions without checkpoints retain their
previously saved totals. Durability depends on the Pi storage surviving power
loss; no application can recover a physically corrupted SD card.

For local testing, `IONIQ_DB_PATH` overrides the default database location.
Run the regression checks with `python3 -m unittest discover -s tests -v`.

## Vehicle diagnostics

The Connection tab offers read-only ECU/fault reports and expanded battery
snapshots for the confirmed 2017 Ioniq Electric 28 kWh Canadian profile. Standard
fault clearing saves a fresh report and requires an explicit operator confirmation
before Mode 04. Community battery definitions remain unvalidated on the physical
vehicle; unsupported ECU services and actuator controls stay unavailable.
See [diagnostic scope, sources, and validation](docs/DIAGNOSTICS.md).
