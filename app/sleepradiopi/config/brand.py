"""The radio's name, as it's spoken and shown.

One place for it, because more than one radio runs this software: the box
radio is "Sleep Radio", the cathedral radio (hardware "cathedral") is
"Phonosphere", and `station_name` in the settings names any radio as you
like. The DJ's welcome and idents, lists ("Friday List on Phonosphere"), the
start-up line, the spoken address, the update announcements, the web page and
the set-up Wi-Fi network all use it. (Recorded jingles are just files: swap
them for ones that say the new name.)

A radio you've named is called that on the network too ("Carisbrooke" is
carisbrooke.local), so several can share a house; one you haven't stays
sleepradiopi. hostname_for() makes the name, apply_hostname() hands it to the
system (root's radio-hostname sets it, at start-up and when it changes).

main.py sets it at start-up; the separate small programs (the start-up sound,
the Wi-Fi manager) read it from the settings file with name_from_config().
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT = "Sleep Radio"
DEFAULT_HOST = "sleepradiopi"
HOST_FILE = Path("/data/radio/hostname")            # read by radio-hostname (root) at start-up
HOST_REQUEST = Path("/run/sleepradiopi/hostname")   # asks it to take a new name up now
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


def hostname_for(settings: dict) -> str:
    """The radio's name on the network: the name you gave it, as a host name
    ("Dad's Radio" -> dads-radio), or sleepradiopi for a radio with no name of
    its own (the hardware's default name doesn't count: every new radio starts
    as sleepradiopi)."""
    custom = settings.get("station_name")
    if isinstance(custom, str) and custom.strip():
        plain = unicodedata.normalize("NFKD", custom).encode("ascii", "ignore").decode().lower()
        slug = re.sub(r"[^a-z0-9]+", "-", plain.replace("'", "")).strip("-")[:63].rstrip("-")
        if slug:
            return slug
    return DEFAULT_HOST


def apply_hostname(wanted: str, file: Path = HOST_FILE, request: Path = HOST_REQUEST,
                   current: str | None = None) -> bool:
    """On the radio: keep the network name for the next start-up, and if the
    system has another one now, ask for the change (True if asked). Nothing on
    a PC, where there's nobody to ask."""
    if not request.parent.is_dir():
        return False
    try:
        if not file.exists() or file.read_text().strip() != wanted:
            tmp = file.with_name(file.name + ".tmp")
            tmp.write_text(wanted + "\n")
            os.replace(tmp, file)
        if (current or socket.gethostname()) != wanted:
            request.touch()
            log.info("network name: asking for %s", wanted)
            return True
    except OSError as e:
        log.warning("network name: couldn't set %s (%s)", wanted, e)
    return False


def set_name(new: str) -> None:
    global name
    name = new


def hotspot_ssid(radio_name: str | None = None) -> str:
    """The set-up network's default name: "SleepRadio-Setup", "Phonosphere-Setup"."""
    n = "".join(ch for ch in (radio_name or name) if ch.isalnum())[:24] or "Radio"
    return f"{n}-Setup"
