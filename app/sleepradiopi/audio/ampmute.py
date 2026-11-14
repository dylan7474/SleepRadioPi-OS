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

import glob
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

LINK = Path("/run/sleepradiopi/amp-mute")
UNMUTE_DELAY_S = 0.12      # after the card is RUNNING: hides its start; a little of the first sound goes unheard
# The card's state (ALSA's /proc status files). Unmuting before the I2S clock has started is what clicks
# (a MAX98357A switched on before its clock: tested 2026-11-14), and a busy Pi at boot can take longer than
# a fixed delay to start the card, so wait until a playback stream says RUNNING, then the delay.
CARD_STATUS = "/proc/asound/card*/pcm*p/sub*/status"
RUNNING_WAIT_S = 3.0       # give up waiting for RUNNING after this (no /proc, a test run): unmute anyway


class AmpMute:
    def __init__(self, path: Path | str = LINK, delay_s: float = UNMUTE_DELAY_S, status: str | None = CARD_STATUS) -> None:
        self.status = status            # glob of ALSA status files; None = don't wait for RUNNING
        self.path = Path(path)
        self.delay_s = delay_s
        self._lock = threading.Lock()
        self._want_sound = True          # the last thing asked for
        self._gen = 0                    # each mute/unmute asked for; a waiting unmute that's been overtaken gives up
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
            self._gen += 1
            self._write("0")

    def unmute_soon(self) -> None:
        """The sound card has just opened: let the sound through once it's really
        playing (a playback stream RUNNING), and a moment after that."""
        with self._lock:
            self._want_sound = True
            self._gen += 1
            gen = self._gen
        threading.Thread(target=self._unmute_when_running, args=(gen,), name="amp-unmute", daemon=True).start()

    def card_running(self) -> bool:
        for f in glob.glob(self.status or ""):
            try:
                with open(f) as fh:
                    if "RUNNING" in fh.readline():
                        return True
            except OSError:
                pass
        return False

    def _unmute_when_running(self, gen: int) -> None:
        end = time.monotonic() + RUNNING_WAIT_S
        while self.status and time.monotonic() < end and not self.card_running():
            if gen != self._gen:
                return
            time.sleep(0.01)
        time.sleep(self.delay_s)
        with self._lock:
            if gen == self._gen and self._want_sound:     # (not closed or asked again meanwhile)
                self._write("1")

    def unmute_now(self) -> None:
        with self._lock:
            self._want_sound = True
            self._gen += 1
            self._write("1")
