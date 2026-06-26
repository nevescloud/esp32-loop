"""esp32loop — the flash→observe→iterate loop, board-agnostic.

  detect          what's plugged in, and which board it looks like
  boards          known boards + their quirks
  flash <board>   build + upload (bundled ble_control example by default)
  watch <board>   capture serial to stdout, non-interactively (the 'observe' half)

`watch` is the load-bearing verb for an agent: it returns the boot log / panic /
advertise line as text instead of trapping you in an interactive monitor.

The observe + control verbs (scan/gatt/read/sub/send/wifi) format esp32loop.runtime,
which returns data and never prints — the same source the MCP server serializes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

from . import boards as B
from . import runtime as R

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PROJECT = REPO / "examples" / "ble_control"

# Source IDF only if a warm shell hasn't already put idf.py on PATH —
# skip the export-script tax otherwise.
IDF_EXPORT = "command -v idf.py >/dev/null 2>&1 || . ~/esp/esp-idf/export.sh >/dev/null"


def _idf(args: list[str], project: Path) -> int:
    cmd = f'{IDF_EXPORT}; idf.py -C "{project}" {" ".join(args)}'
    return subprocess.run(["bash", "-lc", cmd]).returncode


def cmd_boards(_args) -> int:
    for b in R.known_boards():
        print(f"\n\033[1m{b['name']}\033[0m  chip={b['chip']} transport={b['transport']} "
              f"console={b['console']} download={b['download']}")
        for line in b["notes"].splitlines():
            print(f"  {line}")
    return 0


def cmd_detect(args) -> int:
    rows = R.detect()
    if not rows:
        print("no serial ports found. Nothing plugged in?")
        return 0
    print(f"{'port':<28} {'transport':<12} {'bridge':<24} {'serial':<18} board")
    for r in rows:
        bridge = r["bridge"]
        bridge = bridge[:23] + "…" if len(bridge) > 24 else bridge
        print(f"{r['port']:<28} {r['transport']:<12} {bridge:<24} "
              f"{(r['serial'] or '—'):<18} {r['label'] or '—'}")
    if args.probe:
        print("\nprobing chips (resets the board into bootloader; native-USB / "
              "auto-reset boards only)…")
        for r in rows:
            match = r["_match"]
            if match and match["download"] != "auto":
                print(f"  {r['port']}: skipped — {match['name']} needs manual download mode "
                      f"(jumper IO0->GND, tap RST)")
                continue
            chip = _probe_chip(r["port"])
            canon = _canon_chip(chip)
            if match and canon:
                ok = canon == match["chip"]
                print(f"  {r['port']}: {chip}  declared={match['chip']}  "
                      f"{'✓' if ok else '✗ MISMATCH'}")
            else:
                print(f"  {r['port']}: {chip}")
    return 0


def _canon_chip(probed: str) -> str | None:
    """Map an esptool chip string ('ESP32-D0WD-V3', 'ESP32-C3') to a board.toml
    chip family ('esp32', 'esp32c3'), or None when it's not a recognizable chip
    (e.g. 'no response') — so a failed probe doesn't read as a declared mismatch."""
    t = probed.lower()
    if "esp32" not in t:
        return None
    for fam in ("c3", "c6", "s3", "s2", "h2", "c2"):
        if fam in t:
            return "esp32" + fam
    return "esp32"


def _probe_chip(port: str) -> str:
    """esptool chip_id — definitive silicon, but needs the board in download mode."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "esptool", "--port", port, "chip_id"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "probe failed"
    if m := re.search(r"Chip is (\S+)", out):
        return m.group(1)
    if m := re.search(r"Detecting chip type[.\s]*(\S+)", out):
        return m.group(1)
    return "no response (in download mode?)"


def cmd_scan(args) -> int:
    """BLE scan — the RF-side counterpart to `detect`: is firmware advertising?"""
    import asyncio
    rows = asyncio.run(R.scan(args.seconds, args.name))
    if not rows:
        print(f"no BLE devices{' matching ' + repr(args.name) if args.name else ''} found")
        return 0
    print(f"{'rssi':>5}  {'name':<26} address")
    for r in rows:
        print(f"{r['rssi']:>5}  {(r['name'] or '(no name)'):<26} {r['address']}")
    return 0


def cmd_status(args) -> int:
    """Whole-board ground-truth in one call: plugged in? advertising? drivable?
    current telemetry. Collapses detect→scan→gatt→read — the after-flash sanity read.
    Never errors on an absent board; absence is reported, not raised."""
    import asyncio
    label = B.load_board(args.board)["label"]
    st = asyncio.run(R.status(args.board, label, args.timeout))
    print(f"{args.board}  \"{label}\"")
    u = st["usb"]
    print(f"  usb:    " + (f"{u['port']}  ({u['chip'] or '?'}, {u['bridge'] or u['transport']})"
                           if u else "not plugged in"))
    ble = st["ble"]
    print(f"  ble:    " + (f"advertising  rssi {ble['rssi']}  [{ble['address']}]"
                           if ble["advertising"] else "not advertising"))
    if st["gatt"] is not None:
        drives = sorted({d for s in st["gatt"] for ch in s["characteristics"] for d in ch["drives"]})
        print(f"  gatt:   {len(st['gatt'])} svcs · drives {' '.join(drives) or '—'}")
    if st["telemetry"] is not None:
        print(f"  telem:  {st['telemetry']}")
    return 0


def _run_ble(board: str, coro_factory, render) -> int:
    """Resolve the board's advertise name, run a runtime BLE coro against it, and
    either render the result or map runtime's exceptions to the CLI's messages +
    exit code. The single place the BLE verbs share their error idiom."""
    import asyncio
    label = B.load_board(board)["label"]
    try:
        result = asyncio.run(coro_factory(label))
    except R.BoardNotFound:
        print(f"no board advertising '{label}' in range — flashed with a connectable "
              f"firmware (e.g. ble_control) and powered?")
        return 1
    except R.NoCharacteristic as e:
        print(f"{label}: {e}")
        return 1
    return render(label, result)


def cmd_gatt(args) -> int:
    """Connect and list services + characteristics — introspect a live device."""
    def render(label, g) -> int:
        print(f"{label} [{g['address']}]")
        for s in g["services"]:
            print(f"  service {s['uuid']}")
            for ch in s["characteristics"]:
                line = f"    char {ch['uuid']}  [{', '.join(ch['properties'])}]"
                if ch.get("drives"):
                    line += f"  ← {' · '.join(ch['drives'])}"
                print(line)
        return 0
    return _run_ble(args.board, lambda label: R.gatt(label, args.timeout), render)


def cmd_read(args) -> int:
    """Connect and read a characteristic (default: the first readable — telemetry)."""
    def render(label, val) -> int:
        print(val)
        return 0
    return _run_ble(args.board, lambda label: R.read(label, args.char, args.timeout), render)


def cmd_sub(args) -> int:
    """Connect and stream notifications — watch a running board's state live over BLE."""
    print(f"# streaming notifications from board (stop: {args.seconds}s)", file=sys.stderr)
    live = lambda line: print(line, flush=True)  # stream as they arrive
    return _run_ble(
        args.board,
        lambda label: R.subscribe(label, args.char, args.seconds, args.timeout, on_line=live),
        lambda label, lines: 0,  # already printed live
    )


def cmd_send(args) -> int:
    """Connect and write [pin, level] to the board's writable char — full GPIO control."""
    def render(label, _) -> int:
        print(f"{label}: gpio {args.pin} <- {1 if args.level else 0}  (ok)")
        return 0
    return _run_ble(
        args.board,
        lambda label: R.send(label, args.pin, args.level, args.timeout),
        render,
    )


def cmd_wifi(args) -> int:
    """Provision WiFi over BLE — write `ssid\\npass` to the board, then watch its
    status notify until it joins (drives wifi_provision). No SSID baked at flash:
    the board boots unconfigured and gets its network over the BLE link. Creds
    come from the args or, omitted, the ESP32LOOP_WIFI_SSID/_PASSWORD env."""
    try:
        ssid, password = R.wifi_creds(args.ssid, args.password)
    except ValueError as e:
        raise SystemExit(str(e))

    def render(label, res) -> int:
        print(f"{label}: creds for {ssid!r} sent", file=sys.stderr)
        print(res["status"] or f"{label}: no status after {args.wait}s — still connecting?")
        return 0 if res["connected"] else 1
    return _run_ble(
        args.board,
        lambda label: R.provision_wifi(
            label, ssid, password, args.char, args.timeout, args.wait),
        render,
    )


def _capture(port: str, baud: int, seconds: float, until: str | None = None,
             reset: bool = False) -> int:
    """Read serial to stdout for `seconds` (or until `until` matches). Shared by
    `watch` and `flash --watch`. Holds DTR/RTS low so opening the port doesn't
    reset the chip; `reset=True` pulses EN via RTS for a fresh boot."""
    import serial  # pyserial; imported lazily so detect/boards work without it

    s = serial.Serial(port, baud, timeout=0.5)
    s.dtr = False
    s.rts = False
    if reset:
        s.rts = True
        time.sleep(0.1)
        s.rts = False
    pattern = re.compile(until) if until else None
    print(f"# watching {port} @ {baud} "
          f"(stop: {seconds}s{' or /' + until + '/' if pattern else ''})",
          file=sys.stderr)
    deadline = time.monotonic() + seconds
    buf = ""
    try:
        while time.monotonic() < deadline:
            chunk = s.read(4096).decode("utf-8", "replace")
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                if pattern:
                    buf = (buf + chunk)[-4096:]
                    if pattern.search(buf):
                        print(f"\n# matched /{until}/", file=sys.stderr)
                        return 0
    finally:
        s.close()
    return 0


def cmd_flash(args) -> int:
    b = B.load_board(args.board)
    port = B.resolve_port(b, args.port)
    project = Path(args.project).resolve() if args.project else DEFAULT_PROJECT
    if b["download"] == "manual-io0" and not args.yes:
        print(f"\n\033[33m{b['name']} needs MANUAL download mode:\033[0m")
        print("  jumper IO0 -> GND, tap RST, then press Enter. (Ctrl-C to abort.)")
        try:
            input()
        except KeyboardInterrupt:
            return 1
    # Bake the board's identity into the firmware: the example reads
    # ESP32LOOP_NAME as its BLE advertise name + serial banner, so the name
    # lives in one place (board.toml) and surfaces in every discovery channel.
    define = f"-DESP32LOOP_NAME={b['label']}"
    print(f"→ {b['name']} as \"{b['label']}\" ({b['chip']}) on {port} from {project}")
    if rc := _idf([define, "set-target", b["chip"]], project):
        return rc
    if rc := _idf([define, "-p", port, "-b", str(b["baud"]), "flash"], project):
        return rc
    if args.watch is not None:
        # Board just hard-reset into the new firmware — observe, don't reset again.
        return _capture(port, b["baud"], args.watch, reset=False)
    return 0


def cmd_watch(args) -> int:
    b = B.load_board(args.board)
    port = B.resolve_port(b, args.port)
    return _capture(port, b["baud"], args.seconds, args.until, args.reset)


def _replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def cmd_new(args) -> int:
    """Scaffold a flashable firmware project by forking a bundled example — the
    examples double as templates, so the agent gets a working baseline and writes
    only the logic, not the IDF ceremony (CMakeLists, sdkconfig, the name bake)."""
    import shutil

    examples = REPO / "examples"
    tmpl = examples / args.template
    if not (tmpl / "CMakeLists.txt").exists():
        opts = ", ".join(p.name for p in sorted(examples.iterdir()) if p.is_dir())
        raise SystemExit(f"unknown template '{args.template}'. options: {opts}")
    dest = (Path(args.dir).resolve() if args.dir else Path.cwd()) / args.name
    if dest.exists():
        raise SystemExit(f"{dest} already exists")
    # Copy source only — never the build dir or a generated sdkconfig.
    shutil.copytree(tmpl, dest, ignore=shutil.ignore_patterns(
        "build", "build-*", ".cache", "sdkconfig", "sdkconfig.old", "sdkconfig.esp32*",
        "managed_components", "*.lock", "dependencies.lock"))
    old_main = next(dest.glob("main/*_main.c"))
    new_main = dest / "main" / f"{args.name}_main.c"
    old_main.rename(new_main)
    _replace(dest / "CMakeLists.txt", f"project({args.template})", f"project({args.name})")
    _replace(dest / "main" / "CMakeLists.txt", old_main.name, new_main.name)
    print(f"scaffolded {dest}  (forked {args.template})")
    print(f"  edit:  {args.name}/main/{args.name}_main.c")
    print(f"  flash: uv run esp32loop flash <board> --project {dest} --watch")
    return 0


def main() -> None:
    R.load_dotenv()  # repo-root .env -> os.environ (for ESP32LOOP_WIFI_*), real env wins
    p = argparse.ArgumentParser(prog="esp32loop", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("boards", help="list known boards + quirks").set_defaults(fn=cmd_boards)

    nw = sub.add_parser("new", help="scaffold a flashable firmware project from a template")
    nw.add_argument("name")
    nw.add_argument("--template", default="ble_control",
                    help="example to fork: ble_control | wifi_provision (default ble_control)")
    nw.add_argument("--dir", help="parent directory to create it in (default: cwd)")
    nw.set_defaults(fn=cmd_new)

    d = sub.add_parser("detect", help="what's plugged in, and which board it looks like")
    d.add_argument("--probe", action="store_true", help="esptool chip_id (needs download mode)")
    d.set_defaults(fn=cmd_detect)

    sc = sub.add_parser("scan", help="BLE scan — is firmware advertising? (RF-side detect)")
    sc.add_argument("--seconds", type=float, default=6, help="scan window (default 6)")
    sc.add_argument("--name", help="only show advertised names containing this substring")
    sc.set_defaults(fn=cmd_scan)

    st = sub.add_parser("status", help="whole-board ground-truth: plugged in? advertising? drivable? telemetry")
    st.add_argument("board")
    st.add_argument("--timeout", type=float, default=10, help="BLE scan/connect timeout (default 10)")
    st.set_defaults(fn=cmd_status)

    sd = sub.add_parser("send", help="connect over BLE and set a pin — drives ble_control")
    sd.add_argument("board")
    sd.add_argument("pin", type=int, help="GPIO number")
    sd.add_argument("level", type=int, choices=[0, 1], help="0 or 1")
    sd.add_argument("--timeout", type=float, default=10, help="BLE scan timeout (default 10)")
    sd.set_defaults(fn=cmd_send)

    wf = sub.add_parser("wifi", help="provision WiFi over BLE — drives wifi_provision")
    wf.add_argument("board")
    wf.add_argument("ssid", nargs="?", help="WiFi SSID (or set ESP32LOOP_WIFI_SSID)")
    wf.add_argument("password", nargs="?",
                    help="WiFi password (or set ESP32LOOP_WIFI_PASSWORD; omit for open networks)")
    wf.add_argument("--char", help="creds characteristic UUID (substring); default: first writable")
    wf.add_argument("--timeout", type=float, default=10, help="BLE scan timeout (default 10)")
    wf.add_argument("--wait", type=float, default=25, help="seconds to wait for it to join (default 25)")
    wf.set_defaults(fn=cmd_wifi)

    g = sub.add_parser("gatt", help="connect over BLE and list services + characteristics")
    g.add_argument("board")
    g.add_argument("--timeout", type=float, default=10, help="BLE scan timeout (default 10)")
    g.set_defaults(fn=cmd_gatt)

    rd = sub.add_parser("read", help="connect over BLE and read a characteristic")
    rd.add_argument("board")
    rd.add_argument("--char", help="characteristic UUID (substring); default: first readable")
    rd.add_argument("--timeout", type=float, default=10, help="BLE scan timeout (default 10)")
    rd.set_defaults(fn=cmd_read)

    su = sub.add_parser("sub", help="connect over BLE and stream notifications (live state)")
    su.add_argument("board")
    su.add_argument("--char", help="characteristic UUID (substring); default: first notifying")
    su.add_argument("--seconds", type=float, default=8, help="how long to stream (default 8)")
    su.add_argument("--timeout", type=float, default=10, help="BLE scan timeout (default 10)")
    su.set_defaults(fn=cmd_sub)

    f = sub.add_parser("flash", help="build + upload firmware")
    f.add_argument("board")
    f.add_argument("--project", help="IDF project dir (default: bundled ble_control example)")
    f.add_argument("--port", help="override the auto-resolved port")
    f.add_argument("--yes", action="store_true", help="skip the manual-download-mode prompt")
    f.add_argument("--watch", nargs="?", type=float, const=8, default=None, metavar="SECS",
                   help="after flashing, capture serial for SECS (default 8) — flash→observe in one go")
    f.set_defaults(fn=cmd_flash)

    w = sub.add_parser("watch", help="capture serial to stdout, non-interactively")
    w.add_argument("board")
    w.add_argument("--port", help="override the auto-resolved port")
    w.add_argument("--seconds", type=float, default=10, help="capture window (default 10)")
    w.add_argument("--until", help="stop early when this regex matches")
    w.add_argument("--reset", action="store_true",
                   help="pulse EN via RTS to capture a fresh boot (UART-bridge boards)")
    w.set_defaults(fn=cmd_watch)

    args = p.parse_args()
    raise SystemExit(args.fn(args))
