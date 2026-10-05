"""Listen here: the radio heard in a browser instead of from its own speaker.

The speaker is one listener and a browser another (audio/speaker.py), so
moving the sound is three steps: the stream is made for the browser (switched
on for now if it was off -- the setting isn't changed), the browser tunes in,
and only then is the speaker let go, so the show never finds itself with
nobody listening.

While the browser has it, play and pause are about the listening there,
wherever they're pressed -- the knob, a button, the page, the sleep timer
(audio/speaker.py sends them here): paused, the browser is let go and the
speaker stays quiet; played again, the browser tunes back in. The speaker
only comes back when Listen here is switched off, and then it carries on as
things stand: playing if it was playing there, paused if it was paused.

And if the browser just goes -- the laptop's lid shut, the tab killed, the
Wi-Fi dropped -- nobody would be left to say "back to the speaker". So the
page says it's still there every few seconds, and once nothing has been heard
from it (and nobody is tuned in) for GONE_S, the radio takes the sound back
by itself, the same way.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

GONE_S = 20.0                # no word from the browser, and nobody tuned in, for this long: the speaker takes it back
LOOK_S = 2.0


class HearHere:
    def __init__(self, output, speaker=None, clock: Callable[[], float] = time.monotonic,
                 gone_s: float = GONE_S, look_s: float = LOOK_S) -> None:
        self.output, self.speaker, self._clock = output, speaker, clock
        self.gone_s, self.look_s = gone_s, look_s
        self._lock = threading.RLock()
        self.on = False
        self.held = False            # paused, while the browser has it
        self._stream_was = True      # the stream was being made before
        self._seen = 0.0             # when the browser was last there
        self._gen = 0

    def start(self) -> None:
        """A browser is about to tune in (the stream is made for it) -- or, already on, says it's still there."""
        with self._lock:
            self._seen = self._clock()
            if self.on:
                return
            self.on, self.held = True, False
            self._stream_was = bool(self.output.enabled)
            if not self._stream_was:
                self.output.set_enabled(True)
            self._gen += 1
            threading.Thread(target=self._watch, args=(self._gen,), name="hear-here", daemon=True).start()
        log.info("listen here: a browser has the radio")

    def hush(self) -> None:
        """It's heard there now: the speaker goes quiet."""
        with self._lock:
            if self.on and self.speaker is not None:
                self.speaker._pause()

    def hold(self) -> None:
        """Paused: the browser is let go, and the speaker stays as quiet as it was."""
        with self._lock:
            if not self.on or self.held:
                return
            self.held = True
            self.output.drop_listeners()
        log.info("listen here: paused")

    def resume(self) -> None:
        """Played again: the browser tunes back in (the page sees to that)."""
        with self._lock:
            if not self.on or not self.held:
                return
            self.held = False
            self._seen = self._clock()
        log.info("listen here: play")

    def stop(self, why: str = "asked") -> None:
        """Back to the speaker, as things stand: playing, or paused."""
        with self._lock:
            if not self.on:
                return
            self.on, held = False, self.held
            self.held = False
            self._gen += 1
            if self.speaker is not None:
                (self.speaker._pause if held else self.speaker._play)()
            if not self._stream_was:
                self.output.set_enabled(False)
        log.info("listen here: back to the speaker, %s (%s)", "paused" if held else "playing", why)

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
