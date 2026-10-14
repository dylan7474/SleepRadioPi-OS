"""Which reader for receivers is on, kept over a restart.

The readers -- Morse (audio/morse.py), FT8 / FT4 / WSPR (audio/ft8.py), PSK31 /
RTTY (audio/keyboard.py), SSTV / weather fax (audio/pictures.py) -- are one at
a time (web/server.py's _one_reader). Which is on, and how, is the setting
rx_reader: state() says it each time one is switched, and restore() switches
it on again at start-up, so a restart -- an update -- comes back reading what it
was reading, as the receiver comes back where it was tuned (Station.retuned).
"""
from __future__ import annotations

import logging
from typing import Callable

from sleepradiopi.audio import ft8, keyboard, morse, pictures

log = logging.getLogger(__name__)


def state() -> dict | None:
    """The reader that's on, as the setting keeps it: {"reader": "cw" | "ft8" | "keyboard" | "pictures", "mode"?,
    and for FT8 "band", "dial", "own"}; None: none is."""
    if morse.reader.on:
        return {"reader": "cw"}
    if ft8.reader.on:
        return {"reader": "ft8", "mode": ft8.reader.mode, "band": ft8.reader.band, "dial": ft8.reader.dial, "own": ft8.reader.own}
    if keyboard.reader.on:
        return {"reader": "keyboard", "mode": keyboard.reader.mode}
    if pictures.reader.on:
        return {"reader": "pictures", "mode": pictures.reader.mode}
    return None


def restore(saved, playing: Callable[[], str | None] | None = None) -> str | None:
    """What state() said, switched on again -> which reader, or None (none was, or what was kept doesn't make
    sense now: it's left off). playing: the address the radio is playing, for FT8 heard on a receiver of your own."""
    if not isinstance(saved, dict):
        return None
    which, mode = saved.get("reader"), saved.get("mode")
    try:
        if which == "cw":
            morse.reader.switch(True)
        elif which == "ft8":
            if not ft8.can(mode or ft8.reader.mode):
                return None
            band, dial, own = saved.get("band"), saved.get("dial"), saved.get("own")
            dial = int(dial) if isinstance(dial, (int, float)) and not isinstance(dial, bool) and 100_000 <= dial <= 500_000_000 else None
            own = own if isinstance(own, str) and own.startswith(("http://", "https://")) else None
            if own and playing is not None:
                ft8.reader.playing = playing
            ft8.reader.switch(True, mode, band if isinstance(band, str) and band else None, dial, own)
        elif which == "keyboard":
            keyboard.reader.switch(True, mode)
        elif which == "pictures":
            pictures.reader.switch(True, mode)
        else:
            return None
    except (ValueError, TypeError) as e:
        log.warning("readers: %s not switched on again (%s)", which, e)
        return None
    log.info("readers: %s on again, as before the restart", which)
    return which
