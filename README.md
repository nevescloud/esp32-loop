# esp32-loop

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)

**Host-side eyes and hands on a *running* ESP32.** Flashing firmware is the part every
tool has; the edge is everything *after boot* — a real host-side BLE client
(`gatt`/`read`/`sub`/`send`) plus per-board transport modeling, so an agent can observe
and command a board that's actually running, across native-USB or any UART bridge (a
native-USB C3, an FTDI- or CP2102-bridged classic ESP32).

```
   ┌─▶ write ─▶ flash ─▶ observe ─▶ control ─┐
   └─────────────── iterate ◀ ───────────────┘
```

It runs the whole arc — so an agent goes from "I want an ESP32 to do X" to a running,
commandable device on its own. No IDE, no interactive monitor, no hand-written IDF
boilerplate.

```
esp32loop detect                        # what's plugged in: board, transport, chip

esp32loop new   <name>                  # scaffold a firmware project from a template
esp32loop flash <board>                 # build + upload (scaffold, example, any IDF tree)

esp32loop watch <board>                 # capture serial as text — not an interactive monitor
esp32loop scan                          # BLE scan — what's advertising?
esp32loop gatt  <board>                 # connect: list services + characteristics
esp32loop read  <board>                 # connect: read a characteristic (telemetry)
esp32loop sub   <board>                 # connect: stream notifications (live state)
esp32loop send  <board> <pin> <level>   # connect: drive a pin
esp32loop wifi  <board> <ssid> <pass>   # connect: provision WiFi over BLE, watch it join
```

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) and [ESP-IDF](https://docs.espressif.com/projects/esp-idf/)
(v5.5) at `~/esp/esp-idf` — the CLI auto-sources it.

```
uv run esp32loop detect
uv run esp32loop new blinky --template ble_control   # your own firmware, ready to edit
uv run esp32loop flash c3_supermini --project blinky --watch
uv run esp32loop send c3_supermini 8 1               # connect over BLE, drive a pin
```

## Boards

Each board is one `boards/<name>.toml` declaring its chip target, transport, console
location, and download quirks — the single source of truth. Ships with:

| board | chip | transport | console | flashing |
|-------|------|-----------|---------|----------|
| `c3_supermini` | esp32c3 | native USB | native USB | one cable, auto-reset |
| `esp32cam` | esp32 | UART bridge (FTDI) | UART0 | auto-reset (RTS→EN) |
| `esp32_devkit` | esp32 | UART bridge (CP2102) | UART0 | auto-reset, USB-C |

These cover both transports — a harness that handles native-USB *and* external UART
bridges (FTDI, CP2102, …) generalizes to most ESP-IDF boards. Add one by dropping a new
`.toml`; no code change.

## Authoring firmware

Two bundled examples double as templates: **`ble_control`** (control + telemetry over a
BLE GATT service — what `gatt`/`read`/`sub`/`send` drive) and **`wifi_provision`** (boots
with no WiFi config and takes its credentials over BLE, so `wifi` configures the network
without a flash-time secret — put the creds in a gitignored `.env` (copy
`.env.example`), read from `ESP32LOOP_WIFI_SSID` / `ESP32LOOP_WIFI_PASSWORD`, never in
the repo). `new` forks one into a ready-to-flash project — write only the logic:

```
uv run esp32loop new blinky --template ble_control
# edit blinky/main/blinky_main.c, then:
uv run esp32loop flash <board> --project blinky --watch
```

Or point `flash` at any existing IDF project:

```
uv run esp32loop flash <board> --project /path/to/firmware
```

## Agent integration

The repo ships a Claude Code skill (`.claude/skills/esp32-loop/`) that teaches an agent
the whole loop — scaffold, flash, observe, control — and the per-board traps, so it
grounds its claims in `detect`/`watch`/`scan` output instead of guessing. The aim is an
agent that can take an ESP32 and prototype on it end to end, on its own.

For non-Bash clients, the runtime verbs (`scan`/`gatt`/`read`/`sub`/`send`/`wifi`) are
also exposed as an MCP server: `uv run --extra mcp esp32loop-mcp`.

## License

[MIT](LICENSE)
