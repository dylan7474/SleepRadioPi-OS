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
