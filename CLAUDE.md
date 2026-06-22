# esp32-loop

A host-side CLI (plus an optional MCP server) that flashes firmware to an ESP32 and
observes/controls it over USB and BLE. The CLI is the substrate; `.claude/skills/esp32-loop/`
is the Claude-specific adapter.

## Environment
- Python ≥ 3.11 — macOS ships 3.9 and `tomllib` needs 3.11. `uv` manages the env.
- Run the CLI as `uv run esp32loop <verb>`. From another repo: `uv run --project /path/to/esp32-loop esp32loop …`.
- The MCP server is the opt-in `[mcp]` extra (`pip install -e '.[mcp]'`); it wraps the runtime verbs only.

## Commands
- `uv run esp32loop detect [--probe]` — USB ports + matched board + transport; `--probe` confirms silicon vs the declared chip.
- `uv run esp32loop flash <board> [--project DIR] [--watch]` — build + upload (default project: `examples/ble_control`); `--watch` captures serial after.
- `uv run esp32loop watch <board> [--until REGEX] [--reset]` — capture serial to stdout; defaults to NOT resetting the board.
- `uv run esp32loop scan | gatt | read | sub | send | wifi <board>` — BLE: advertise check · introspect · read · stream notifications · set a GPIO · provision WiFi.
- `uv run esp32loop new <name> [--template ble_control|wifi_provision]` — scaffold a flashable firmware from a bundled example.
- `uv run esp32loop boards` — list known boards and their quirks.

## Architecture
- `esp32loop/runtime.py` — verb logic, print-free, returns data; the single source both front-ends share.
- `esp32loop/cli.py` / `esp32loop/mcp.py` — thin front-ends (CLI formats for a terminal; MCP serializes JSON-RPC and must not touch stdout).
- `boards/<name>.toml` — per-board data. The CLI is data-driven: a new board is a new `.toml`, no code change.
- `examples/<name>/` — bundled firmware; these double as `new` templates and as `flash`'s default.

## Gotchas (non-obvious, fail silently)
- **`board.toml` is a board *type*, not a unit** — never bake a per-unit USB serial; `bridge` keys by USB-UART chip *model*. Identical units separate only by `--port`.
- **`console` ≠ `transport`** — native-USB boards log over USB; UART-bridge boards log over UART0. Watch the wrong port and a healthy board shows nothing.
- **`download` is per-board** — native-USB auto-enters download mode; a 4-wire FTDI needs the manual IO0→GND + RST dance (`flash` prompts unless `--yes`).
- **BLE verbs bind by GATT *property*, not a fixed service UUID** (`runtime._pick`: first writable/notify/readable). "Drivable" = a firmware exposing the right characteristic shape; `gatt` annotates which verb drives each char (`drives`).
- **WiFi credentials resolve from `ESP32LOOP_WIFI_SSID` / `ESP32LOOP_WIFI_PASSWORD`** (a gitignored `.env`; copy `.env.example`), never from `board.toml`.

## Testing
There is no mock suite — verification is on real hardware. After a change, exercise it against a
board (`detect`, `flash --watch`, `scan`/`gatt`/`sub`), and confirm behavior from the output.
