# Diagnostic scope and validation

Target vehicle: **2017 Hyundai Ioniq Electric, 28 kWh, Canada** (confirmed by
owner). No physical vehicle or ELM327 adapter was available during implementation.

## Standard OBD diagnostics

The Connection page queries a fixed list of read services through the same worker
as telemetry: supported Mode 01 and Mode 09 PID bitmaps, VIN where returned,
stored faults (03), pending faults (07), and permanent faults (0A). Responding CAN
addresses are displayed individually. This is an overview of observed responders,
not a claim to enumerate every module. BMS identity is a community mapping.
Hyundai-specific ECU fault services are not implemented. Missing, truncated, or
unsupported responses never mean “no faults.” Raw replies are saved in each
SQLite report alongside parsed results, timestamp, and adapter address.

The decoder supports 11-bit ISO 15765 CAN headers and reassembles interleaved
ISO-TP frames. It checks sequence numbers, message length, and CAN DTC counts.
Only sourced descriptions are shown: P0133 is defined in the ELM327 manual;
other codes explicitly say that a verified definition is unavailable. Manufacturer
code descriptions require Hyundai service information before being added.

Reference: [ELM327 datasheet, pages 34–35 and 42–43](https://www.elmelectronics.com/wp-content/uploads/2016/07/ELM327DS.pdf).

## Clearing workflow

“Review fault clearing” requires fresh zero-speed, non-charging telemetry and
saves a new diagnostic report. A valid stored-fault response is required. A
five-minute, single-use confirmation token binds the request to that report and
the exact current connection. The confirmation dialog requires the operator to
verify parked/READY-off/ignition conditions; zero speed alone cannot prove them.
The worker rechecks telemetry before sending standard Mode 04. It persists an
audit intent before sending and records ECU acknowledgments afterward. A lost
reply becomes `unconfirmed`; the UI instructs the operator to reread rather than
automatically retry. Reconnecting invalidates existing confirmation tokens.

Mode 04 addresses standard OBD responders, not every Hyundai ECU. It can erase
freeze frames and diagnostic test information and reset readiness. Permanent
faults are cleared by the ECU, not Mode 04. No real fault-clearing command was
sent during development; all clearing verification used fake connections.

Reports are downloadable from `/api/diagnostics/reports/<id>` and retained in the
existing trip SQLite database. `/api/diagnostics/reports` lists the most recent
50 reports. Reports can contain VINs; they remain local to this application.

## Expanded readings

The read-only button requests BMS 2101–2105 at header 7E4 and VMCU 2101 at 7E2.
Hardcoded, reviewed formulas support 96 cell voltages, complete-pack imbalance,
12 battery-module temperatures, inlet temperature, minimum/maximum cell voltage,
motor RPM, accelerator pedal position, and gear indicator bits. It never executes
CSV expressions. Missing cell blocks produce nulls and an unavailable imbalance,
not a false zero. Cell voltages outside the source range (2.8–4.2 V) and module
temperatures outside −40–80 °C are marked unavailable. Readings are timestamped
snapshots, not a continuously fresh stream, and are saved as reports.

The bundled CSVs are source evidence from
[JejuSoul’s Ioniq 28 kWh PID definitions](https://github.com/JejuSoul/OBD-PIDs-for-HKMC-EVs/tree/master/Ioniq%20EV%20-%2028kWh/extendedpids),
retrieved October 11, 2026. Local copies use normalized LF line endings.
Original upstream Git blob IDs:

- BMS data: `ce55524442ce78dd32433124ab35baf0068157cc`
- Cell data: `4b99ffd885d62cd989f54fd1b1392197db46c74f`
- VMCU data: `8929f08ac20bea056104b492ebb523d901f5733c`

These are community definitions and remain **unvalidated on this vehicle**.
Validate the adapter’s actual framing and raw payload lengths, compare SOC,
voltage, temperatures, speed and gear against trusted diagnostics, and inspect
all 96 cell readings before marking the profile validated. No automatic profile
validation or actuator promotion is implemented.

## Service controls

The Connection page now includes a source-pinned ECU/PID browser and a fixed
allowlist of temporary IGPM/BCM body controls. Commands require fresh stationary
telemetry and two explicit operator confirmations, execute on the sole OBD worker,
and record an audit before activation. Persistent charge limits and locking
preferences can be saved as local requests and investigated through before/after
read captures; actual persistent vehicle writes remain unavailable.
See [vehicle management scope and acceptance gates](vehicle/README.md).

## Verification

Run `python3 -m unittest discover -s tests -v` with Flask and pexpect installed.
Tests cover freshness, session classification, checkpoint recovery, Bluetooth
worker ownership, interleaved/truncated ISO-TP, CAN DTC decoding, unsupported
responses, report persistence, confirmation expiry/replay/reconnection, and
unconfirmed clearing outcomes. Browser checks used a separate fake-adapter
server and disposable SQLite database. GPIO, physical RFCOMM, exact vehicle
readings, and real ECU clearing remain hardware acceptance checks.
