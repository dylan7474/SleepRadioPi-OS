"""Mute the amplifier while nothing plays.

The MiniAmp hisses (and makes odd noises) while it idles powered on, even with
the sound card closed. Its MUTE pin (GPIO16: low = muted, high = playing)
silences it. The speaker mutes just before it closes the sound card and
unmutes a moment after it has opened it, so the card's own open/close pops
are hidden too.

The pin is exported by sleepradiopi-station (as root, when the setting
`amp_mute_gpio` names it) and its value file is linked at LINK for the
station's user. Anything missing or failing leaves the amp as it was: a radio
without the pin, or a desktop test run, just plays. If a write fails the amp
is NOT left muted by us on purpose: the worst case is the old hiss.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

LINK = Path("/run/sleepradiopi/amp-mute")
UNMUTE_DELAY_S = 0.12      # after the card opens: hides its open pop; a little of the first sound goes unheard


class AmpMute:
    def __init__(self, path: Path | str = LINK, delay_s: float = UNMUTE_DELAY_S) -> None:
        self.path = Path(path)
        self.delay_s = delay_s
        self._lock = threading.Lock()
        self._want_sound = True          # the last thing asked for
        self._timer: threading.Timer | None = None
        self._failed = False

    @property
    def available(self) -> bool:
        return self.path.exists() and not self._failed

    def _write(self, value: str) -> None:
        if self._failed:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY)     # (never creates the file: a missing pin stays missing)
            try:
                os.write(fd, value.encode())
            finally:
                os.close(fd)
        except OSError as e:
            if self.path.exists():           # (a missing pin is normal: say nothing)
                log.warning("amp mute: can't write %s (%s); leaving the amp alone", self.path, e)
            self._failed = True

    def mute(self) -> None:
        """The sound card is closing: silence the amp now."""
        with self._lock:
            self._want_sound = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._write("0")

    def unmute_soon(self) -> None:
        """The sound card has just opened: let the sound through after the pop."""
        with self._lock:
            self._want_sound = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay_s, self._unmute)
            self._timer.daemon = True
            self._timer.start()

    def _unmute(self) -> None:
        with self._lock:
            if self._want_sound:             # (not closed again in the meantime)
                self._write("1")
            self._timer = None

    def unmute_now(self) -> None:
        with self._lock:
            self._want_sound = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._write("1")
