#!/usr/bin/env python3
"""DME / VMCU, BMS, and Controller Readout Utility for Hyundai Ioniq 2017 EV.
Polls live telemetry, full BMS pack data (96 cells, 12 thermal sensors),
and Powertrain Controller (VMCU/Inverter) data via the running local OBD service.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

# ANSI color codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def fetch_api(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    url = f"http://127.0.0.1:5000{endpoint}"
    payload = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"{RED}Error communicating with OBD service: {e}{RESET}")
        sys.exit(1)


def format_value(value, spec=""):
    """Display unavailable telemetry without inventing a numeric reading."""
    return "Unavailable" if value is None else format(value, spec)


def format_cell_grid(cells):
    if not cells:
        return f"{DIM}No cell voltages available{RESET}"
    lines = []
    # Print 96 cells in 6 rows of 16 cells each
    lines.append(f"{DIM}Cell  | " + " ".join(f"{i:02d}" for i in range(1, 17)) + f"{RESET}")
    lines.append(f"{DIM}------|-" + "---" * 16 + f"{RESET}")
    for row in range(0, len(cells), 16):
        row_cells = cells[row:row+16]
        row_label = f"{row+1:02d}-{row+len(row_cells):02d} |"
        val_strs = []
        for c in row_cells:
            if c is None:
                val_strs.append(f"{DIM}--{RESET}")
            else:
                # Color code cell voltage: green if within normal, yellow if slight deviation
                val_strs.append(f"{c:.2f}")
        lines.append(f"{row_label} " + " ".join(val_strs))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Hyundai Ioniq EV Full DME / BMS / Controller Readout")
    parser.add_argument("--json", action="store_true", help="Output raw JSON format")
    args = parser.parse_args()

    print(f"{CYAN}{BOLD}Querying vehicle ECUs (BMS 7E4, VMCU 7E2, LDC 7E5)...{RESET}")

    status = fetch_api("/api/status")
    expanded = fetch_api("/api/diagnostics/expanded", method="POST")
    dtcs = fetch_api("/api/diagnostics/read", method="POST")

    if args.json:
        combined = {
            "status": status,
            "expanded_diagnostics": expanded,
            "dtc_report": dtcs
        }
        print(json.dumps(combined, indent=2))
        return

    obd = status.get("obd", {})
    readings = expanded.get("readings", {})
    cells = readings.get("cell_voltages_v", [])
    valid_cells = [c for c in cells if c is not None]
    temps = readings.get("module_temperatures_c", [])
    valid_temps = [t for t in temps if t is not None]

    print("\n" + "=" * 68)
    print(f"{BOLD}{GREEN}  HYUNDAI IONIQ EV (2017) - FULL DME / BMS / CONTROLLER READOUT{RESET}")
    print("=" * 68)

    # 1. DME / VMCU / Motor Controller
    print(f"\n{BOLD}{CYAN}1. POWERTRAIN CONTROLLER (DME / VMCU / INVERTER - Header 7E2){RESET}")
    print("-" * 68)
    gear_flags = readings.get("gear_flags") or {}
    active_gears = [g for g, active in gear_flags.items() if active]
    gear_str = active_gears[0] if active_gears else "Park" if obd.get("speed_kmh") == 0 else "Unknown"

    motor_rpm = readings.get("motor_rpm") if readings.get("motor_rpm") is not None else obd.get("motor_rpm")
    pedal = readings.get("accelerator_pedal_pct")
    speed = obd.get("speed_kmh")

    print(f"  • Selector Position     : {BOLD}{gear_str}{RESET}")
    print(f"  • Vehicle Speed         : {BOLD}{format_value(speed, '.1f')} km/h{RESET}")
    print(f"  • Motor Speed (RPM)     : {BOLD}{format_value(motor_rpm)} RPM{RESET}")
    print(f"  • Accelerator Pedal     : {BOLD}{format_value(pedal, '.1f')}%{RESET}")
    print(f"  • Controller Protocol   : KWP2000 / ISO 15765-4 CAN (500 kbps)")

    # 2. Battery Management System (BMS)
    print(f"\n{BOLD}{CYAN}2. BATTERY MANAGEMENT SYSTEM (BMS / BECM - Header 7E4){RESET}")
    print("-" * 68)
    soc_disp = obd.get("soc_display")
    soc_bms = obd.get("soc_bms")
    soh = obd.get("soh")
    hv_volt = obd.get("battery_voltage")
    hv_curr = obd.get("battery_current")
    hv_pwr = obd.get("battery_power_kw")
    max_chg = obd.get("max_charge_power_kw")
    max_dis = obd.get("max_discharge_power_kw")
    aux_v = obd.get("aux_voltage")
    cec = obd.get("cum_energy_charged_kwh")
    ced = obd.get("cum_energy_discharged_kwh")
    inlet_t = readings.get("inlet_temperature_c")

    print(f"  • State of Charge (SOC) : {BOLD}{format_value(soc_disp, '.1f')}%{RESET} (Cluster) | {BOLD}{format_value(soc_bms, '.1f')}%{RESET} (BMS Internal)")
    print(f"  • State of Health (SOH) : {BOLD}{GREEN}{format_value(soh, '.1f')}%{RESET}")
    print(f"  • Pack High Voltage     : {BOLD}{format_value(hv_volt, '.1f')} V{RESET}")
    print(f"  • Pack Current          : {BOLD}{format_value(hv_curr, '+.1f')} A{RESET}")
    print(f"  • Instantaneous Power   : {BOLD}{format_value(hv_pwr, '+.2f')} kW{RESET}")
    print(f"  • Allowed Charge Limit  : {BOLD}{format_value(max_chg, '.1f')} kW{RESET}")
    print(f"  • Allowed Discharge Lim : {BOLD}{format_value(max_dis, '.1f')} kW{RESET}")
    print(f"  • Aux 12V System Voltage: {BOLD}{format_value(aux_v, '.1f')} V{RESET}")
    print(f"  • Cumulative Energy In  : {format_value(cec, ',.1f')} kWh (CEC)")
    print(f"  • Cumulative Energy Out : {format_value(ced, ',.1f')} kWh (CED)")

    # Thermals
    print(f"\n  {BOLD}Thermal Telemetry:{RESET}")
    if valid_temps:
        print(f"  • Pack Temp Min / Max   : {min(valid_temps)} °C / {max(valid_temps)} °C (Avg: {sum(valid_temps)/len(valid_temps):.1f} °C)")
        if inlet_t is not None:
            print(f"  • Air Inlet Temperature : {inlet_t} °C")
        temp_strs = [f"M{i+1}:{t}°C" for i, t in enumerate(temps) if t is not None]
        print(f"  • 12 Module Probes      : " + ", ".join(temp_strs))

    # Cell Voltages
    print(f"\n  {BOLD}96-Cell Pack Balance & Voltage Map:{RESET}")
    if valid_cells:
        min_c = min(valid_cells)
        max_c = max(valid_cells)
        delta_mv = (max_c - min_c) * 1000.0
        avg_c = sum(valid_cells) / len(valid_cells)
        imbalance_color = GREEN if delta_mv < 20 else YELLOW if delta_mv < 40 else RED
        print(f"  • Cell Min / Max / Avg  : {min_c:.3f} V / {max_c:.3f} V / {avg_c:.3f} V")
        print(f"  • Cell Imbalance (Delta): {imbalance_color}{BOLD}{delta_mv:.1f} mV{RESET}")
        print()
        print(format_cell_grid(cells))

    # 3. Auxiliary Low DC-DC Converter (LDC)
    print(f"\n{BOLD}{CYAN}3. LOW DC-DC CONVERTER (LDC - Header 7E5){RESET}")
    print("-" * 68)
    print(f"  • 12V Output Voltage    : {BOLD}{format_value(aux_v, '.1f')} V{RESET}")
    print("  • LDC Converter State   : Unavailable (not queried)")

    # 4. Diagnostic Trouble Codes (DTCs)
    print(f"\n{BOLD}{CYAN}4. DIAGNOSTIC TROUBLE CODES (DTCs){RESET}")
    print("-" * 68)
    modules = dtcs.get("modules", {})
    all_codes = []
    coverage = []
    valid_reads = 0
    for ecu_id, module in modules.items():
        faults = module.get("faults", {})
        for kind in ("03", "07", "0A"):
            result = faults.get(kind)
            if not isinstance(result, dict) or result.get("status") != "ok":
                coverage.append(f"{ecu_id} Mode {kind}: unavailable")
                continue
            valid_reads += 1
            for code in result.get("codes") or []:
                all_codes.append((ecu_id, kind, code.get("code"), code.get("description")))
        if isinstance(faults.get("status"), str):
            coverage.append(f"{ecu_id}: {faults['status']}")

    for ecu, kind, code, desc in all_codes:
        kind_str = {"03": "Confirmed", "07": "Pending", "0A": "Permanent Stored"}[kind]
        print(f"  • ECU {BOLD}{ecu}{RESET} [{kind_str}]: {BOLD}{YELLOW}{code}{RESET} - {desc}")
    if not modules:
        print("  Fault coverage unavailable: no responding ECUs.")
    elif not valid_reads:
        print("  Fault coverage unavailable: no valid standard OBD responses.")
    elif not all_codes:
        print("  No fault codes in valid standard OBD responses.")
    for message in coverage:
        print(f"  • Fault coverage: {message}")
    print("  Hyundai-specific ECU faults are not queried.")

    print("\n" + "=" * 68 + "\n")


if __name__ == "__main__":
    main()
