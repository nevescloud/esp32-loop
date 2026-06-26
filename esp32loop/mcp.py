"""esp32-loop MCP server — the runtime control verbs over the Model Context
Protocol, so an MCP client (Claude Desktop, a hosted agent) gets hands on a board
the same way the CLI does. Every tool wraps esp32loop.runtime, which returns data
and never touches stdout — required, because a stdio MCP server speaks JSON-RPC
over stdout and any stray print corrupts the stream.

Scope is deliberately the *live runtime* half of the loop: detect/boards + scan/
gatt/read/subscribe/send/wifi. Build/flash/watch stay in the CLI — they need a
local toolchain and a USB-attached board, which Bash already covers and a remote
client cannot drive. This is the local backend; the same verb surface is what a
hosted (Worker + Durable Object) front door re-exposes over WiFi.

  uv run --extra mcp esp32loop-mcp      # stdio
"""

from __future__ import annotations

from . import boards as B
from . import runtime as R

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # extra not installed — fail with guidance, not a stack trace
    raise SystemExit("the MCP server needs the 'mcp' extra — run `uv sync --extra mcp`")

mcp = FastMCP("esp32-loop")


def _label(board: str) -> str:
    """Resolve a board handle to its advertised name. B.load_board raises
    SystemExit on an unknown handle — fatal in a server, so remap to ValueError,
    which FastMCP returns as a normal tool error."""
    try:
        return B.load_board(board)["label"]
    except SystemExit as e:
        raise ValueError(str(e))


@mcp.tool()
def boards() -> list[dict]:
    """List known board types and their quirks (chip, transport, console, download
    mode). Each `name` is the handle the other tools take as `board`."""
    return R.known_boards()


@mcp.tool()
def detect() -> list[dict]:
    """What's physically plugged in over USB, and which known board each port looks
    like. Host-side observation, no chip cooperation. (Chip probing needs download
    mode and stays in the CLI.)"""
    return [{k: v for k, v in r.items() if k != "_match"} for r in R.detect()]


@mcp.tool()
async def scan(seconds: float = 6, name: str | None = None) -> list[dict]:
    """BLE scan: is any firmware advertising? The RF-side counterpart to detect.
    Returns advertised name + address + RSSI, strongest signal first. `name`
    filters to advertised names containing that substring."""
    return await R.scan(seconds, name)


@mcp.tool()
async def status(board: str, timeout: float = 10) -> dict:
    """Whole-board ground-truth in one call — the after-flash sanity read. Collapses
    detect + scan + gatt + read into one structured result: USB presence (port, chip),
    BLE advertising (rssi, address), the live GATT surface with `drives`, and a
    telemetry snapshot. Each layer degrades to null independently, so an absent or
    half-up board is reported, never an error. Prefer this over four separate calls
    when you just need to know the board's current state. `board` is a name from
    `boards`."""
    return await R.status(board, _label(board), timeout)


@mcp.tool()
async def gatt(board: str, timeout: float = 10) -> dict:
    """Connect over BLE and list the board's GATT services + characteristics, each
    with its properties (read/write/notify) and `drives` — the verbs that bind to it
    (`send`/`wifi` → first writable, `sub` → first notify, `read` → first readable).
    `drives` tells you which characteristic to drive without guessing. Introspect a
    live device before reading or driving it. `board` is a name from `boards`."""
    return await R.gatt(_label(board), timeout)


@mcp.tool()
async def read(board: str, char: str | None = None, timeout: float = 10) -> str:
    """Connect over BLE and read a characteristic (default: the first readable —
    usually telemetry). `char` is a UUID substring to target a specific one."""
    return await R.read(_label(board), char, timeout)


@mcp.tool()
async def subscribe(board: str, char: str | None = None,
                    seconds: float = 8, timeout: float = 10) -> list[str]:
    """Connect over BLE and collect notifications for `seconds` — a window of a
    running board's live state. Returns the decoded lines received."""
    return await R.subscribe(_label(board), char, seconds, timeout)


@mcp.tool()
async def send(board: str, pin: int, level: int, timeout: float = 10) -> str:
    """Connect over BLE and drive a GPIO pin: write [pin, level] to the board's
    writable characteristic. level is 0 or 1. The control half of the loop."""
    label = _label(board)
    await R.send(label, pin, level, timeout)
    return f"{label}: gpio {pin} <- {1 if level else 0} (ok)"


@mcp.tool()
async def provision_wifi(board: str, ssid: str | None = None, password: str | None = None,
                         char: str | None = None, timeout: float = 10,
                         wait: float = 25) -> dict:
    """Hand WiFi creds to the board over BLE (no SSID baked at flash), then watch
    its status until it joins. The bridge to cloud control: once on WiFi the board
    can dial out to a remote relay. Omit ssid/password to use the server's
    ESP32LOOP_WIFI_SSID / ESP32LOOP_WIFI_PASSWORD env — the secret then never enters
    this conversation. Returns {connected, status}."""
    return await R.provision_wifi(_label(board), ssid, password, char, timeout, wait)


def main() -> None:
    R.load_dotenv()  # repo-root .env -> os.environ, so an agent can omit WiFi creds
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
