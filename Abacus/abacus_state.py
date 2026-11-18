"""Switches that can change while Abacus is running (by voice), shared between the main loop
and the MCP tool server through one small JSON file.

config.MOUSE_CONTROL is the default. Saying "Abacus, hands off my mouse" / "Abacus, use my mouse"
overrides it and the choice survives restarts, until you say otherwise or edit MOUSE_CONTROL
(a changed config value always wins over an older spoken choice)."""
import json
from pathlib import Path

import config

STATE_FILE = Path(__file__).resolve().parent / "state.json"


def _default_mouse() -> bool:
    return bool(getattr(config, "MOUSE_CONTROL", True))


def _read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def mouse_allowed() -> bool:
    """True = Abacus may drive the real mouse pointer (the default)."""
    data = _read()
    if "mouse" in data and data.get("mouse_config") == _default_mouse():
        return bool(data["mouse"])
    return _default_mouse()


def set_mouse(allowed: bool) -> None:
    data = _read()
    data.update(mouse=bool(allowed), mouse_config=_default_mouse())
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), "utf-8")
    tmp.replace(STATE_FILE)
