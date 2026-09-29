"""The radio's name, as it's spoken and shown.

One place for it, because more than one radio runs this software: the box
radio is "Sleep Radio", the cathedral radio (hardware "cathedral") is
"Phonosphere", and `station_name` in the settings names any radio as you
like. The DJ's welcome and idents, lists ("Friday List on Phonosphere"), the
start-up line, the spoken address, the update announcements, the web page and
the set-up Wi-Fi network all use it. (Recorded jingles are just files: swap
them for ones that say the new name.)

main.py sets it at start-up; the separate small programs (the start-up sound,
the Wi-Fi manager) read it from the settings file with name_from_config().
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT = "Sleep Radio"
BY_HARDWARE = {"cathedral": "Phonosphere"}

name = DEFAULT


def name_for(settings: dict) -> str:
    """The name the settings give: station_name if set, else the hardware's default."""
    custom = settings.get("station_name")
    if isinstance(custom, str) and custom.strip():
        return " ".join(custom.split())[:40]
    return BY_HARDWARE.get(settings.get("hardware"), DEFAULT)


def name_from_config(path: Path) -> str:
    try:
        return name_for(json.loads(Path(path).read_text()))
    except (OSError, ValueError, AttributeError):
        return DEFAULT


def set_name(new: str) -> None:
    global name
    name = new


def hotspot_ssid(radio_name: str | None = None) -> str:
    """The set-up network's default name: "SleepRadio-Setup", "Phonosphere-Setup"."""
    n = "".join(ch for ch in (radio_name or name) if ch.isalnum())[:24] or "Radio"
    return f"{n}-Setup"
