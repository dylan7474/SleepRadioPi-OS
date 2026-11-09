"""Notifications: which alerts the radio may say, and while it is doing what.

Each kind of alert has one of three modes:
    "off"     muted: never
    "active"  armed while the radio is playing (not while it is paused)
    "always"  armed while it is playing or paused
and the quiet hours (the do-not-disturb) beat every mode: nothing gets past them.

An alert is spoken by Flite (tts/flite.py), not the DJ's neural voice: it needs no model in memory (the DJ's
worker wants 150-260 MB, which a Pi Zero 2 W can't spare), it works with the DJ off, and its sentences are made
from the figures as they are ("a storm 12 km to the north-east"). It plays like the lightning bell does: over
whatever is playing, or on its own while paused (control.play_clip).

Lightning is the one kind whose mode lives beside its bell (lightning_when): the bell rings when a storm
first comes near, and main._lightning_bell says the distance after it. The rest are found here, by Notifier,
from conditions.alerts_now(): each is said once when it starts or goes higher, and the same one not again for
ALERT_AGAIN_S (as the DJ's alerts were). An alert held back by its mode or the quiet hours is not marked as
said, so it's still said once the radio is in a state that allows it, if it is still going on.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

MODES = ("off", "active", "always")
EVERY_S = 30.0

# kind (the start of an alert's key) -> what it's called on the page
KINDS: dict[str, str] = {
    "lightning": "Lightning near here",
    "aurora": "Aurora",
    "es": "VHF openings (sporadic E)",
    "noaa": "Solar storms and radio blackouts",
    "quake": "Earthquakes, world",
    "ukquake": "Earthquakes, UK",
    "met": "Met Office warnings",
    "flood": "Flood warnings",
    "qrz": "New country in the QRZ logbook",
    "aircraft": "Aircraft",
}

# what a kind is until the owner sets it: what the old Alerts switch (Band conditions) said, which was spoken by
# the DJ between songs, so while playing
modes: dict[str, str] = {}
notifier: "Notifier | None" = None
DEFAULT_MODE = "off"


def set_default(alerts_were_on: bool) -> None:
    global DEFAULT_MODE
    DEFAULT_MODE = "active" if alerts_were_on else "off"


def mode(kind: str) -> str:
    m = modes.get(kind, DEFAULT_MODE)
    return m if m in MODES else DEFAULT_MODE


def allowed(m: str, paused: bool) -> bool:
    """Armed in this state? (the quiet hours are a separate, stronger rule)"""
    return m == "always" or (m == "active" and not paused)


def clean(given: dict | None) -> dict:
    """The saved modes with anything unknown dropped."""
    return {k: v for k, v in (given or {}).items() if k in KINDS and v in MODES}


def kind_of(what: str) -> str:
    return what.partition(":")[0]


class Notifier:
    """A thread that looks at the alerts every EVERY_S and says the ones that are armed.

    found()   -> conditions.alerts_now() as it is now ([(what, level, words, short)])
    paused()  -> is the radio paused
    quiet()   -> is it the quiet hours
    clock_ok()-> the clock is right (an alert has its times)
    say(words, label) -> make the sentence and play it
    """

    def __init__(self, found: Callable[[], list], paused: Callable[[], bool], quiet: Callable[[], bool],
                 clock_ok: Callable[[], bool], say: Callable[[str, str], None], again_s: float = 3 * 3600.0) -> None:
        self.found, self.paused, self.quiet, self.clock_ok, self.say = found, paused, quiet, clock_ok, say
        self.again_s = again_s
        self._alerted: dict[str, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> list[str]:
        """One look. Returns the words said (empty if none)."""
        now = time.time() if now is None else now
        found = [f for f in self.found() if kind_of(f[0]) != "lightning" and kind_of(f[0]) in KINDS]
        live = {what for what, *_ in found}
        for what in [w for w in self._alerted if w not in live and now - self._alerted[w][1] > self.again_s]:
            del self._alerted[what]                  # (ended: said again if it comes back after a while)
        if not found or self.quiet() or not self.clock_ok():
            return []
        paused = self.paused()
        said: list[str] = []
        labels: list[str] = []
        for what, level, words, _short in found:
            kind = kind_of(what)
            if not allowed(mode(kind), paused):
                continue
            before = self._alerted.get(what)
            if before is None or level > before[0]:
                self._alerted[what] = (level, now)
                said.append(words)
                labels.append(KINDS[kind])
        if said:
            try:
                self.say(" ".join(said), labels[0] if len(labels) == 1 else "Alerts")
            except Exception:                        # (an alert that can't be said must not stop the radio)
                log.exception("notify: couldn't say %r", " ".join(said)[:80])
        return said

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="notify", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(EVERY_S):
            try:
                self.tick()
            except Exception:
                log.exception("notify: a look at the alerts failed")

    def stop(self) -> None:
        self._stop.set()
