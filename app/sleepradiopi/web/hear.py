"""Listen here: the radio heard in a browser instead of from its own speaker.

The speaker is one listener and a browser another (audio/speaker.py), so
moving the sound is three steps: the stream is made for the browser (switched
on for now if it was off -- the setting isn't changed), the browser tunes in,
and only then is the speaker let go, so the show never finds itself with
nobody listening. Afterwards the speaker comes back and the stream goes off
again if it was off.

And if the browser just goes -- the laptop's lid shut, the tab killed, the
Wi-Fi dropped -- nobody would be left to say "back to the speaker", so the
radio watches: once nobody has been tuned in for GONE_S, it takes the sound
back by itself.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

GONE_S = 20.0                # nobody tuned in for this long: the speaker takes it back
LOOK_S = 2.0


class HearHere:
    def __init__(self, output, speaker=None, clock: Callable[[], float] = time.monotonic,
                 gone_s: float = GONE_S, look_s: float = LOOK_S) -> None:
        self.output, self.speaker, self._clock = output, speaker, clock
        self.gone_s, self.look_s = gone_s, look_s
        self._lock = threading.RLock()
        self.on = False
        self._stream_was = True      # the stream was being made before
        self._playing_was = False    # the speaker was playing before
        self._hushed = False         # ...and has been let go
        self._seen = 0.0             # when somebody was last tuned in
        self._gen = 0

    def start(self) -> None:
        """A browser is about to tune in: the stream is made for it."""
        with self._lock:
            if self.on:
                self._seen = self._clock()
                return
            self.on, self._hushed = True, False
            self._stream_was = bool(self.output.enabled)
            self._playing_was = self.speaker is not None and not self.speaker.paused
            if not self._stream_was:
                self.output.set_enabled(True)
            self._seen = self._clock()
            self._gen += 1
            threading.Thread(target=self._watch, args=(self._gen,), name="hear-here", daemon=True).start()
        log.info("listen here: a browser has the radio")

    def hush(self) -> None:
        """It's heard there now: the speaker goes quiet."""
        with self._lock:
            if not self.on or self._hushed:
                return
            self._hushed = True
            if self.speaker is not None and self._playing_was:
                self.speaker.pause()

    def stop(self, why: str = "asked") -> None:
        """Back to the speaker (and the stream off again, if it was)."""
        with self._lock:
            if not self.on:
                return
            self.on = False
            self._gen += 1
            if self._hushed and self._playing_was and self.speaker is not None:
                self.speaker.play()
            if not self._stream_was:
                self.output.set_enabled(False)
        log.info("listen here: back to the speaker (%s)", why)

    def _watch(self, gen: int) -> None:
        while True:
            time.sleep(self.look_s)
            with self._lock:
                if not self.on or gen != self._gen:
                    return
                now = self._clock()
                if self.output.listeners() > 0:
                    self._seen = now
                elif now - self._seen > self.gone_s:
                    self.stop("the browser went away")
                    return
