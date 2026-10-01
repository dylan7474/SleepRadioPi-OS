"""The knob pressed and turned: back or on through what's playing.

A book or podcast moves SEEK_BOOK_MS a click, a song from an album or
playlist SEEK_SONG_MS (Station.knob_seek); a quick spin goes further a
click. Playing, it just jumps. Paused, nothing would say where it's got to,
so a snatch of it (SNATCH_MS) plays every SNATCH_EVERY_S while it moves,
the radio staying paused -- like a CD player's scan. A podcast is streamed:
it moves, but there's nothing to hear until it plays.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

SNATCH_EVERY_S = 0.5
SNATCH_MS = 300
IDLE_S = 1.5          # no turn for this long: the snatches stop
SPEED = ((0.08, 4), (0.2, 2))   # seconds since the last click -> clicks it counts as


class KnobSeek:
    def __init__(self, station, control, make_clip) -> None:
        self.station, self.control, self.make_clip = station, control, make_clip
        self._lock = threading.Lock()
        self._last_click = 0.0
        self._want = None             # (path, ms): where to play a snatch from
        self._heard = None
        self._moved_at = 0.0
        self._thread: threading.Thread | None = None

    def turn(self, clicks: int, now: float | None = None, speed_up: bool = True) -> None:
        now = time.monotonic() if now is None else now
        gap = now - self._last_click
        self._last_click = now
        per = next((n for limit, n in SPEED if gap < limit), 1) if speed_up else 1
        try:
            where = self.station.knob_seek(clicks * per)
        except ValueError as e:
            log.info("knob seek: %s", e)
            return
        if where is None or where["path"] is None or not self.control.paused:
            return
        with self._lock:
            self._want, self._moved_at = (where["path"], where["ms"]), now
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._snatches, name="knob-seek", daemon=True)
                self._thread.start()

    def _snatches(self) -> None:
        while True:
            with self._lock:
                want = self._want
                if want == self._heard and time.monotonic() - self._moved_at > IDLE_S:
                    self._thread = None
                    return
            if want != self._heard and self.control.paused:
                self._heard = want
                try:
                    audio = self.station.snatch(want[0], want[1], SNATCH_MS)
                    if audio is not None:
                        self.control.play_clip(self.make_clip(audio, "seek", "Finding the place"))
                except Exception:
                    log.exception("knob seek: couldn't play the snatch")
            time.sleep(SNATCH_EVERY_S)
