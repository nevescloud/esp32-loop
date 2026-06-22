---
name: esp32-loop
description: Use when authoring, flashing, observing, or controlling firmware on an ESP32 on the bench — scaffolding a new firmware, flashing it, reading the serial/boot log, checking BLE advertising, driving a board over BLE, or diagnosing why one won't boot/advertise/respond. Drives the esp32loop CLI as a write→flash→observe→control loop across boards and transports (native-USB vs UART-bridge).
user-invocable: false
---

# esp32-loop

The substrate gives you **eyes and hands on the chip**: observe what a board does
(`detect`/`watch`/`scan`) and command it (`new`/`flash`/`send`). The failure mode
without it: you guess from a connect dialog and burn a session. Ground every claim in
`detect`/`watch`/`scan` output — never infer board state from a port name alone.

## The loop

```
esp32loop detect                     # 1. ground: what's plugged in, which board, transport
esp32loop new <name> --template …    # 2. author: scaffold a firmware, write only the logic
esp32loop flash <board> --project <name> --watch   # 3. install + observe in one go
esp32loop send <board> <pin> <level>               # 4. control it over BLE
# read the capture → edit main/<name>_main.c → back to 3
```

Run all CLI commands with `uv run esp32loop …` (the repo pins Python ≥3.11; macOS
ships 3.9). From another repo, `uv run --project /path/to/esp32-loop esp32loop …`.

## Verbs

- `detect [--probe]` — host-side: lists each port's transport class, USB bridge,
  serial, and the matching board. `--probe` runs `esptool chip_id` for definitive
  silicon AND cross-checks it against the board's declared `chip` (flags a
  `✗ MISMATCH` — the guard against eyeballing silicon from peripherals); it resets
  the board into the bootloader, so it skips `manual-io0` boards instead of hanging.
- `scan [--seconds N] [--name SUBSTR]` — BLE scan, the RF-side counterpart to `detect`:
  lists advertised name / address / RSSI, strongest first. The in-tool answer to "is my
  firmware actually advertising?" — the question `detect`/`watch` can't reach. Filter
  with `--name esp32` so the room's other beacons don't bury yours.
- `gatt <board>` — connect and list the device's services + characteristics. Introspect
  what a board exposes before reading or driving it.
- `read <board> [--char UUID]` — connect and read a characteristic (default: the first
  readable — `ble_control`'s telemetry JSON).
- `sub <board> [--char UUID] [--seconds N]` — connect and stream notifications: watch a
  running board's live state over BLE. The observe-after-boot edge.
- `send <board> <pin> <level>` — connect over BLE and set a GPIO (a 2-byte `(pin, level)`
  write to `ble_control`'s command char). Full output control without a script. The
  `gatt`/`read`/`sub`/`send` family needs the board running a connectable firmware.
- `wifi <board> [ssid] [password] [--wait N]` — provision WiFi *over the BLE link*: writes
  `ssid\npass` to `wifi_provision`'s creds char, then watches its status notify until it
  reports `connected` (prints the IP) or `failed`. No SSID baked at flash — the board
  boots unconfigured and gets its network over BLE, then persists it to NVS. Creds come
  from the args or, omitted, the `ESP32LOOP_WIFI_SSID` / `ESP32LOOP_WIFI_PASSWORD` env —
  prefer omitting, so the password stays out of the transcript. This is the BLE-as-hands
  payoff: a transport the agent already drives configures a second one.
- `new <name> [--template ble_control|wifi_provision] [--dir DIR]` — scaffold a
  flashable IDF project by forking a bundled example (the examples double as templates).
  Renames the source + project; write only the logic in `main/<name>_main.c`, then
  `flash --project <name>`. Removes the CMakeLists/sdkconfig/name-bake ceremony.
- `flash <board> [--project DIR] [--port P] [--yes] [--watch [SECS]]` — `--project`
  points at any IDF project (a scaffold, a bundled example, or an external tree).
  `--watch` captures serial right after upload — flash→observe in one command. Default
  target is the bundled `examples/ble_control`.
- `watch <board> [--seconds N] [--until REGEX] [--reset]` — **the load-bearing verb.**
  Captures serial to stdout so you read it as text. Defaults to NOT resetting the chip
  on open (holds DTR/RTS in the run state) — observing never disturbs a running board.
  `--until` stops early on a match (e.g. `--until 'advertising as'`). `--reset` pulses
  EN to capture a fresh boot banner (UART/FTDI boards). On boards that wire RTS->EN,
  resetting on open looks like a bootloop — which is exactly why no-reset is the default.

## Per-board facts are not here

Chip target, console location, and the download dance live in `boards/<board>.toml` —
the single source of truth. Read them with `esp32loop boards`. Two traps they encode:

- **Console follows transport.** A native-USB board logs over its USB port; a UART-bridge
  board logs over UART0 = the bridge. Watching the wrong port shows nothing on a board
  that's working fine.
- **Download mode varies by board.** Native-USB and auto-reset bridges enter it on their
  own; a bare 4-wire bridge needs jumper IO0→GND + tap RST. `board.toml`'s `download`
  field says which; `flash` prompts on `manual-io0` unless you pass `--yes`.

## Adding a board

Drop a `boards/<name>.toml` (copy an existing one), set `chip` / `transport` /
`console` / `download` / `port_glob`, and write the gotchas into `[notes]`. No code
change — the CLI is board-data-driven. The toml is a board *type*, never a unit: if
two boards share a transport family (both `cu.usbserial-*`), add `bridge = "FT232R"`
(the USB-UART chip model — a type, not a serial) to narrow it. Identical units are
separable only by `--port`; never bake a per-unit serial into the type file.
