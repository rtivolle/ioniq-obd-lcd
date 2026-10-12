# Vehicle management and persistent settings

The current profile targets the 2017 Hyundai Ioniq Electric 28 kWh Canadian vehicle.
No physical vehicle or adapter was available during implementation.

`ioniq-2017.json` is a normalized subset of the public-domain (Unlicense)
[canair profile](https://github.com/philipkocanda/canair/tree/92f742d9deaa69be77af1d16d08cf6a3436e1627/profiles/ioniq-2017/ecus),
revision `92f742d9deaa69be77af1d16d08cf6a3436e1627`, retrieved October 12, 2026.
ECU descriptions identify the source vehicle's equipment; a listed module need not
exist on another trim. The catalog includes 31 ECU addresses, 73 read requests,
and 51 named actuator definitions. Only 24 IGPM/BCM commands with upstream verified
labels and documented operating states are exposed for execution. Other commands
remain visibly unavailable. Community verification is not validation on this car.
Source formulas are displayed as reference only; they are never evaluated or
presented as decoded live values. Imported historical identities/part numbers are
not asserted as the identity of the connected vehicle.

## Reads and temporary controls

The Connection page exposes the catalog and one selected read at a time. Replies
are parsed with ISO-TP validation and matched to the expected physical response
address and service prefix. Raw responses and definitions are saved to the
existing diagnostic report database. No arbitrary command endpoint exists.

All adapter traffic stays on the existing OBD worker. Reads/controls invalidate
the published telemetry snapshot and reset integration across their gap. ELM
flow-control routing is configured for the physical ECU, then restored to automatic
flow control. Header formatting is restored afterward. Unsupported adapter commands
abort the operation. Some modules may not respond with a generic ELM327 clone.

Controls require fresh stationary, non-charging telemetry, explicit profile and
vehicle-condition confirmation, a documented non-READY/non-charging state, and a
second operation confirmation. The token is single-use, expires after 60 seconds,
and binds to the current connection. No accessory relay is automatically enabled.
For BCM controls the operator must establish the required accessory/ignition power;
no power-state inference or automatic ignition control is implemented.

An audit intent is persisted before entering the extended session or actuating.
A confirmed activation lasts 0.5 seconds before an explicit return-control request.
Lost activation replies still trigger release. The default diagnostic session is
also requested, including when release fails. Failed cleanup closes the adapter
and records an unconfirmed outcome. Reception acknowledgment does not prove physical
completion. There is no automatic activation retry. Process termination/power loss
can prevent release; a physical acceptance test must verify ECU session expiry and
normal behavior after reconnect before operational use.

## Persistent settings: support started, vehicle writes unavailable

AC/DC charge limits, automatic locking and automatic unlocking have typed desired
values, SQLite persistence, capability/status responses, and UI controls. Supported
choice names describe requested behavior, not proven options on this vehicle.
Requests are keyed to this fixed profile, not yet a verified VIN.

`POST /api/vehicle/settings` stores requested values **locally only**. It does not
limit charging, lock doors or write an ECU. Current vehicle values remain null;
read/write support is false and saved requests remain `pending_protocol`.
`POST /api/vehicle/settings/<setting>/apply` returns 409 before queuing any traffic.
There is no silent fallback from a persistent preference to a temporary actuator.

The evidence workflow reads candidate pages before and after an operator manually
changes one setting in the vehicle menus. Charge-limit requests collect VCU 21F2;
locking preferences collect BCM 22B001/22B009. These pages are research candidates,
not verified locations for those settings. Missing responses remain unavailable.
After-capture reports list differing ISO-TP payload bytes and reference the matching
before report and adapter. Live telemetry can also change bytes; differences neither
identify a setting nor authorize writing it. If a menu option does not exist, do not
assume an OBD command can add it.

Enabling real persistent writes requires:

1. Matching ECU identity/firmware and verified vehicle profile.
2. Documented read and write services, DID/local identifier, format, ranges, and
   preservation of unrelated bits, backed by Hyundai service information or
   repeatable vehicle captures. Do not guess programming sessions or security keys.
3. Confirmed stationary operating prerequisites and session/access requirements.
4. Capture of the original value, durable audit intent, one explicitly confirmed
   write, independent readback, and a documented recovery procedure.
5. Physical tests for reconnects, rejected writes, lost replies, and power loss.

The default registry deliberately contains no persistent write protocol. Source
research did not establish one for these settings. See also the
[OVMS Ioniq 28 kWh support matrix](https://docs.openvehicles.com/en/stable/components/vehicle_hyundai_ioniqvfl/docs/index.html),
which does not implement charge control or locking.

## Validation

Run `python3 -m unittest discover -s tests -v`. Tests use fake connections and
cover allowlists, worker ownership, confirmations, expiry/replay/reconnection,
activation/release/session cleanup, unavailable responses, audit persistence,
setting validation and reload, raw before/after comparisons, and refusal to queue
unsupported vehicle writes. Browser checks use a disposable database with GPIO
initialization disabled and no OBD worker. The sample preview changes memory only.
