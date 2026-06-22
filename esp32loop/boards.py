"""Board registry. A board.toml is the single source of truth for one board's
chip target, transport, console location, and download quirks — flash-time
config, one declarative file per board."""

from __future__ import annotations

import glob
import tomllib
from pathlib import Path

from . import transport as T

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"


def list_boards() -> list[str]:
    return sorted(p.stem for p in BOARDS_DIR.glob("*.toml"))


def load_board(name: str) -> dict:
    path = BOARDS_DIR / f"{name}.toml"
    if not path.exists():
        raise SystemExit(
            f"unknown board '{name}'. known: {', '.join(list_boards()) or '(none)'}"
        )
    data = tomllib.loads(path.read_text())
    return data["board"] | {
        "name": name,  # the CLI handle (the .toml filename)
        # Human identity: shown in detect/scan, baked into firmware at flash
        # time. Defaults to the handle when no [identity] name is set.
        "label": data.get("identity", {}).get("name", name),
        # Optional USB-UART bridge MODEL (e.g. "FT232R", "CP2102") — a board-TYPE
        # fact, reusable across units, that narrows a shared transport family to
        # the right board. NOT a per-unit serial (that'd make the type file
        # instance-specific). None when the family glob already resolves uniquely.
        "bridge": data["board"].get("bridge"),
        "notes": data.get("notes", {}).get("text", "").strip(),
    }


def resolve_port(board: dict, override: str | None = None) -> str:
    if override:
        return override
    matches = sorted(glob.glob(board["port_glob"]))
    if not matches:
        raise SystemExit(
            f"no port matching {board['port_glob']} for '{board['name']}'. "
            f"Plugged in? Run `esp32loop detect`."
        )
    # port_glob picks the transport family; an optional `bridge` model narrows a
    # shared family (two cu.usbserial-* boards) to the right one — via the same
    # T.bridge_matches rule `detect` uses, so the two can't disagree on identity.
    # Enforced when ioreg gives us devices, so a single glob hit on the WRONG board
    # errors instead of silently flashing it (one glob hit is NOT proof of identity).
    if board.get("bridge"):
        devices = T.usb_devices()
        if devices:  # have data — the hint is authoritative
            narrowed = [p for p in matches if T.bridge_matches(board, p, devices)]
            if not narrowed:
                seen = [T.bridge_name(p, devices) for p in matches]
                raise SystemExit(
                    f"no '{board['bridge']}'-bridge port for '{board['name']}' (saw {seen}). "
                    f"Wrong board on this port, or it's unplugged?"
                )
            matches = narrowed
        # else: off macOS / ioreg unavailable — fall through to glob + ambiguity
    if len(matches) > 1:
        raise SystemExit(
            f"ambiguous: {matches} match '{board['name']}'. Run `esp32loop detect` "
            f"and pass --port — identical boards differ only by serial/port."
        )
    return matches[0]
