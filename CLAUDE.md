# esp32-loop

A host-side CLI (plus an optional MCP server) that flashes firmware to an ESP32 and
observes/controls it over USB and BLE. The CLI is the substrate; `.claude/skills/esp32-loop/`
is the Claude-specific adapter.

## How the agent drives a board (design axis)
The universal "any action" primitive is the **write→flash→observe loop** — an agent
authoring firmware and flashing it *is* arbitrary on-device capability, just slow
(~25 s/cycle) and state-resetting. The runtime verbs (`send`/`wifi`/`sub`/…) are a
**fast, stateful cache** over that loop, not the source of power: a verb earns its place
only when an action is frequent enough that reflashing is too slow, or stateful enough that
a reflash would wipe what must stay held. Otherwise the action is just firmware to flash.

So capability is already unbounded; the scarce resources are **loop latency,
observability, and self-description**. Invest there — faster flash/reset, structured board
state, clean panic/boot-reason surfacing, `gatt`-style self-describing surfaces — not in an
on-device REPL/eval or a speculatively growing verb set. Let a real task the loop *can't*
express specify the next runtime verb; don't add capability ahead of one.

## Environment
- Python ≥ 3.11 — macOS ships 3.9 and `tomllib` needs 3.11. `uv` manages the env.
- Run the CLI as `uv run esp32loop <verb>`. From another repo: `uv run --project /path/to/esp32-loop esp32loop …`.
- The MCP server is the opt-in `[mcp]` extra, run as `uv run --extra mcp esp32loop-mcp`; it wraps the runtime verbs only.

## Commands
Verb surface: `detect · flash · watch · scan · gatt · read · sub · send · wifi · new · boards`,
all run as `uv run esp32loop <verb>`. Signatures, flags, and per-verb notes live in the skill
(`.claude/skills/esp32-loop/SKILL.md`) — the canonical agent reference; don't restate flags
here, they drift.

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
