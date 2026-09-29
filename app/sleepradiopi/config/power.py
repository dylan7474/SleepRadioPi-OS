"""Shutting the radio down from its web page.

The station doesn't run as root, so it can't power the Pi off itself.
SLEEPRADIOPI_POWER_REQUEST names a file the station creates to ask for a
shutdown; something running as root watches for it and runs poweroff (the
appliance image does this for /run/sleepradiopi/poweroff). Without the
variable, e.g. on a desktop, there's no shutdown option.
"""

import os
from pathlib import Path

ENV = "SLEEPRADIOPI_POWER_REQUEST"


def _request_file() -> Path | None:
    path = os.environ.get(ENV)
    return Path(path) if path else None


def can_power_off() -> bool:
    path = _request_file()
    return path is not None and path.parent.is_dir()


def request_power_off() -> bool:
    """Ask for a shutdown. Returns False if this system doesn't offer one."""
    if not can_power_off():
        return False
    _request_file().touch()
    return True


def request_rollback() -> bool:
    """Ask the root helper to boot the previous version (the other root slot).
    False if this system doesn't offer it."""
    if not can_power_off():
        return False
    parent = _request_file().parent
    (parent / "rollback-status").unlink(missing_ok=True)
    (parent / "rollback").touch()
    return True


def rollback_status(wait_s: float = 20) -> dict | None:
    """What the root helper said about a rollback ({"ok": bool}), waiting up to wait_s."""
    import json
    import time
    path = _request_file().parent / "rollback-status" if _request_file() else None
    end = time.monotonic() + wait_s
    while path is not None and time.monotonic() < end:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.5)
    return None


def request_restart() -> bool:
    """Ask for a restart (a "reboot" file beside the shutdown one; the same
    root helper watches both). False if this system doesn't offer one."""
    if not can_power_off():
        return False
    (_request_file().parent / "reboot").touch()
    return True
