"""Runtime verbs as data — the observe + control half of the loop, *returned*
rather than printed, so two callers can share one source: the CLI (which formats
for a terminal) and the MCP server (which serializes to JSON-RPC and must never
touch stdout). BLE orchestration and the board-match rule live here; presentation
lives in the caller. Failures are exceptions, not exit codes — BoardNotFound /
NoCharacteristic — which each caller renders in its own idiom.

This is the substrate's `board.toml is the source of truth` ethos applied to verb
logic: the connect→pick→read/write dance exists once, not once per front-end."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import boards as B
from . import transport as T

WIFI_SSID_ENV = "ESP32LOOP_WIFI_SSID"
WIFI_PASS_ENV = "ESP32LOOP_WIFI_PASSWORD"


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a `.env` file (repo root by default) for keys not
    already set — an explicitly-exported var still wins. Minimal KEY=VALUE parser:
    skips blanks / `#` comments, strips a leading `export` and surrounding quotes.
    The creds live here (gitignored), never in the repo. No-op if the file is
    absent, so this is safe to call unconditionally from each entry point."""
    path = path or (Path(__file__).resolve().parent.parent / ".env")
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, val = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


class BoardNotFound(Exception):
    """No board advertising the expected name was in range."""


class NoCharacteristic(Exception):
    """The connected board exposes no characteristic matching the request."""


# ── host-side observation (no board cooperation, no BLE) ──────────────────────

def known_boards() -> list[dict]:
    """Known board types + their quirks (the `boards` verb, as data)."""
    return [
        {"name": b["name"], "label": b["label"], "chip": b["chip"],
         "transport": b["transport"], "console": b["console"],
         "download": b["download"], "notes": b["notes"]}
        for b in (B.load_board(n) for n in B.list_boards())
    ]


def match_board(port: str, boards: list[dict], devices: dict) -> dict | None:
    """The board a port belongs to. port_glob picks the transport family; an
    optional `bridge` model narrows a shared family via T.bridge_matches — the
    same rule resolve_port uses, so detect and flash can't disagree on identity."""
    glob_hits = [b for b in boards if Path(port).match(Path(b["port_glob"]).name)]
    hinted = next((b for b in glob_hits if T.bridge_matches(b, port, devices)), None)
    return hinted or next((b for b in glob_hits if not b.get("bridge")), None)


def detect() -> list[dict]:
    """What's plugged in over USB, and which board each port looks like. One row
    per serial port. No chip probe — that needs download mode and stays in the
    CLI; each row carries the matched board under `_match` for that probe pass."""
    devices = T.usb_devices()
    boards = [B.load_board(n) for n in B.list_boards()]
    rows = []
    for p in T.list_ports():
        m = match_board(p, boards, devices)
        rows.append({
            "port": p, "transport": T.classify(p),
            "bridge": T.bridge_name(p, devices), "serial": T.port_serial(p, devices),
            "board": m["name"] if m else None, "label": m["label"] if m else None,
            "_match": m,
        })
    return rows


# ── BLE: scan, then connect and run a verb ────────────────────────────────────

def decode(data) -> str:
    """Telemetry is JSON/text; fall back to hex for binary. Whitespace (newlines,
    tabs) counts as text — a record terminated with CRLF is still text, not binary,
    so `isprintable()` alone (which rejects all control chars) is too strict."""
    try:
        s = bytes(data).decode("utf-8")
        if all(c.isprintable() or c in "\r\n\t" for c in s):
            return s
    except UnicodeDecodeError:
        pass
    return bytes(data).hex()


async def _device(label: str, timeout: float):
    """Find a board by its advertised name (robust against macOS cached names)."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return next((d for d, adv in found.values() if adv.local_name == label), None)


def _pick(client, char: str | None, prop: str):
    """A characteristic by UUID substring, else the first with property `prop`."""
    for s in client.services:
        for ch in s.characteristics:
            if char:
                if char.lower() in str(ch.uuid).lower():
                    return ch
            elif prop in ch.properties:
                return ch
    return None


async def _with_client(label: str, timeout: float, fn):
    """Scan → connect → run fn(client, dev). The print-free orchestration every
    BLE verb shares; raises BoardNotFound when nothing answers to `label`."""
    from bleak import BleakClient
    dev = await _device(label, timeout)
    if dev is None:
        raise BoardNotFound(label)
    async with BleakClient(dev) as c:
        return await fn(c, dev)


async def scan(seconds: float = 6, name: str | None = None) -> list[dict]:
    """BLE scan: is firmware advertising? The RF-side counterpart to detect.
    Returns {rssi, name, address}, strongest signal first."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = [
        {"rssi": adv.rssi, "name": adv.local_name or "", "address": addr}
        for addr, (_dev, adv) in found.items()
        if not name or (adv.local_name and name.lower() in adv.local_name.lower())
    ]
    rows.sort(key=lambda r: r["rssi"], reverse=True)
    return rows


def _introspect(client) -> list[dict]:
    """Services + characteristics of a connected device, each char annotated with
    `drives`: the verbs that would bind to it under the same first-by-property rule
    `_pick` uses (`send`/`wifi` → first writable, `sub` → first notify, `read` →
    first readable). The contract made visible — shared by `gatt` and `status`."""
    PICK = [("write", ["send", "wifi"]), ("notify", ["sub"]), ("read", ["read"])]
    claimed: set[str] = set()  # each property's first holder wins, matching _pick
    services = []
    for s in client.services:
        chars = []
        for ch in s.characteristics:
            drives = [v for prop, verbs in PICK
                      if prop in ch.properties and prop not in claimed for v in verbs]
            claimed.update(prop for prop, _ in PICK
                           if prop in ch.properties and prop not in claimed)
            chars.append({"uuid": str(ch.uuid),
                          "properties": sorted(ch.properties), "drives": drives})
        services.append({"uuid": str(s.uuid), "characteristics": chars})
    return services


async def gatt(label: str, timeout: float = 10) -> dict:
    """Connect and introspect services + characteristics of a live device. Each
    char carries `drives` (see `_introspect`) — whether a firmware is drivable and
    how, so no separate protocol spec has to describe it."""
    async def show(c, dev):
        return {"address": dev.address, "services": _introspect(c)}
    return await _with_client(label, timeout, show)


async def status(board: str, label: str, timeout: float = 10) -> dict:
    """One-call board ground-truth, collapsing the detect→scan→gatt→read confirm
    dance into a single structured read — the observability primitive the loop leans
    on after every flash. Layers, each degrading independently to None/absent so the
    call never raises: USB presence (`detect`) + BLE advertising (`scan`) + live GATT
    surface and a telemetry snapshot (one connection). `gatt`/`telemetry` stay None
    when the board isn't advertising or drops before connect — absence is the answer,
    not an error."""
    row = next((r for r in detect() if r["board"] == board), None)
    usb = row and {"port": row["port"], "transport": row["transport"],
                   "bridge": row["bridge"], "serial": row["serial"],
                   "chip": row["_match"]["chip"] if row["_match"] else None}
    adv = next((h for h in await scan(timeout, label) if h["name"] == label), None)
    out = {"board": board, "label": label, "usb": usb or None,
           "ble": {"advertising": adv is not None,
                   "rssi": adv["rssi"] if adv else None,
                   "address": adv["address"] if adv else None},
           "gatt": None, "telemetry": None}
    if adv is None:
        return out

    async def probe(c, dev):
        ch = _pick(c, None, "read")
        telem = decode(await c.read_gatt_char(ch)) if ch is not None else None
        return _introspect(c), telem
    try:
        out["gatt"], out["telemetry"] = await _with_client(label, timeout, probe)
    except BoardNotFound:
        pass  # advertised a moment ago but dropped before connect — leave ble as-is
    return out


async def read(label: str, char: str | None = None, timeout: float = 10) -> str:
    """Connect and read a characteristic (default: the first readable)."""
    async def do(c, dev):
        ch = _pick(c, char, "read")
        if ch is None:
            raise NoCharacteristic(
                f"no readable characteristic{' matching ' + char if char else ''}")
        return decode(await c.read_gatt_char(ch))
    return await _with_client(label, timeout, do)


async def subscribe(label: str, char: str | None = None, seconds: float = 8,
                    timeout: float = 10, on_line=None) -> list[str]:
    """Connect and collect notifications for `seconds` — live state over BLE.
    `on_line`, if given, is called per notification as it arrives (the CLI streams
    live; the MCP just takes the returned list)."""
    async def do(c, dev):
        ch = _pick(c, char, "notify")
        if ch is None:
            raise NoCharacteristic(
                f"no notifying characteristic{' matching ' + char if char else ''}")
        lines: list[str] = []

        def cb(_s, data):
            line = decode(data)
            lines.append(line)
            if on_line:
                on_line(line)
        await c.start_notify(ch, cb)
        await asyncio.sleep(seconds)
        await c.stop_notify(ch)
        return lines
    return await _with_client(label, timeout, do)


async def send(label: str, pin: int, level: int, timeout: float = 10) -> None:
    """Connect and write [pin, level] to the board's writable char — GPIO control.
    Raises NoCharacteristic if the board exposes nothing writable."""
    async def do(c, dev):
        wch = _pick(c, None, "write")
        if wch is None:
            raise NoCharacteristic("connected, but it exposes no writable characteristic")
        await c.write_gatt_char(wch, bytes([pin & 0xFF, 1 if level else 0]),
                                response="write" in wch.properties)
    await _with_client(label, timeout, do)


def wifi_creds(ssid: str | None, password: str | None) -> tuple[str, str]:
    """Resolve WiFi creds: an explicit arg wins, else the `ESP32LOOP_WIFI_SSID` /
    `ESP32LOOP_WIFI_PASSWORD` env vars. Raises ValueError when no SSID is found
    anywhere. The credential's home is the environment, never `board.toml` (a board
    *type*, committed, non-secret) — so it stays out of the repo, and an MCP agent
    can omit it: the server reads its own env and the password never enters the
    model's context. Each front-end maps the ValueError to its own idiom."""
    ssid = ssid or os.environ.get(WIFI_SSID_ENV)
    password = password or os.environ.get(WIFI_PASS_ENV, "")
    if not ssid:
        raise ValueError(
            f"no WiFi SSID — pass one or set {WIFI_SSID_ENV} (+ {WIFI_PASS_ENV} for the password).")
    return ssid, password


async def provision_wifi(label: str, ssid: str | None = None, password: str | None = None,
                         char: str | None = None, timeout: float = 10,
                         wait: float = 25) -> dict:
    """Write `ssid\\npass` to the board's writable char (no SSID baked at flash),
    then watch its status notify until it joins or fails. `ssid`/`password` fall
    back to the env (see `wifi_creds`). Returns {connected: bool|None, status:
    <last line>} — connected is None when the board has no status char to confirm
    against (fire-and-forget)."""
    ssid, password = wifi_creds(ssid, password)
    creds = f"{ssid}\n{password}".encode()

    async def do(c, dev):
        wch = _pick(c, char, "write")
        if wch is None:
            raise NoCharacteristic("connected, but it exposes no writable characteristic")
        # Subscribe to status first, so we don't miss the join the write triggers.
        sch = _pick(c, None, "notify")
        done, last = asyncio.Event(), {"line": None}
        if sch is not None:
            def on_status(_s, data):
                last["line"] = decode(data)
                if '"connected"' in last["line"] or '"failed"' in last["line"]:
                    done.set()
            await c.start_notify(sch, on_status)
        await c.write_gatt_char(wch, creds, response="write" in wch.properties)
        if sch is None:
            return {"connected": None, "status": None}  # fire-and-forget
        try:
            await asyncio.wait_for(done.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        await c.stop_notify(sch)
        return {"connected": bool(last["line"] and '"connected"' in last["line"]),
                "status": last["line"]}
    return await _with_client(label, timeout, do)
