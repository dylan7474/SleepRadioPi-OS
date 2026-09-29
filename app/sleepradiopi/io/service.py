"""The service menu: hold preset buttons 1 and 4 together for 5 seconds.

For getting a radio back when its page can't be reached (a new Wi-Fi, a
forgotten password, an update gone wrong) -- no screen, so the DJ talks you
through it:

    1  restart the radio
    2  reset the Wi-Fi: forget the networks added on the page, and make the
       radio's own network (SleepRadio-Setup) to set a new one up
    3  a status report (address, Wi-Fi, version, free space); then press 3
       again to go back to the previous version of the software
    4  factory reset: every setting back to how it came (Wi-Fi and the
       page's password too), keeping the music, audiobooks and voices --
       confirmed by pressing 2, then 3

Doing nothing for a while leaves the menu. While it's open the buttons
belong to it (io/presets.py hands their presses over), so nothing plays and
no preset is overwritten.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

HOLD_S = 5.0           # both buttons held this long opens the menu
WAIT_S = 45.0          # no press this long after the last words end closes it
TICK_S = 1.0

MENU = ("Service menu. Press one to restart the radio. Two to reset the Wi-Fi. "
        "Three for a status report. Four for a factory reset. Or press nothing, to leave.")
CLOSED = "Service menu closed."
RESTARTING = "Rebooting."
WIFI = ("Resetting the Wi-Fi. I'll forget the networks added on the web page, and make my own network, "
        "so you can set up a new one.")
ROLLBACK_ASK = "To go back to the previous version of the software, press three again. Or press nothing, to leave."
ROLLBACK = "Going back to the previous version of the software. Rebooting."
NO_ROLLBACK = "There's no previous version on this radio to go back to, so nothing has changed."
RESET_ASK = ("Factory reset. This puts every setting back to how it came, including the Wi-Fi and the web "
             "page's password. Your music, audiobooks and voices are kept. To confirm, press two, then three. "
             "Or press nothing, to leave.")
RESET_NEXT = "Now press three."
RESETTING = "Resetting all the settings. Restarting, then I'll tell you how to set me up."
CANCELLED = "Cancelled. Nothing has changed."


class ServiceMenu:
    """say(text) speaks (a beep first; the DJ's voice made in the background);
    prepare(text) makes a line ahead of time. actions: "wifi", "status" (->
    the words), and the ones that end with a restart -- "restart", "rollback"
    (-> True if it's happening), "reset" -- which get their words: they go
    quiet at once, say them, and only then act (no music in between)."""

    def __init__(self, say: Callable[[str], None], actions: dict, prepare: Callable[[str], None] = lambda t: None,
                 wait_s: float = WAIT_S, clock=time.monotonic, busy: Callable[[], bool] = lambda: False) -> None:
        """busy(): still making or saying the words (the wait starts after)."""
        self.say, self.prepare, self.actions, self.busy = say, prepare, actions, busy
        self._status: str | None = None     # the status report's words, made ahead
        self.wait_s, self.clock = wait_s, clock
        self.state: str | None = None         # None (closed), "menu", "rollback?", "reset?", "reset2"
        self._lock = threading.Lock()
        self._deadline = 0.0
        self._timer: threading.Timer | None = None

    @property
    def active(self) -> bool:
        return self.state is not None

    # --- opening it (the two-button hold) -------------------------------------------------

    def holding(self) -> None:
        """Both buttons have gone down: a beep so you know it's counting, and
        the menu's words made now, so they're ready when it opens."""
        log.info("service menu: buttons 1 and 4 held")
        self._status = None
        self._prepare_all()

    def open(self) -> None:
        log.info("service menu: open")
        self._to("menu", MENU)
        if self._status is None:           # (opened from the page: no hold to make them in)
            self._prepare_all()

    def _prepare_all(self) -> None:
        """Make the menu's words and the status report now (slow on a Zero), so
        a press speaks at once."""
        def run():
            self.prepare(MENU)
            self.prepare(RESTARTING)
            try:
                self._status = f"{self.actions['status']()} {ROLLBACK_ASK}"
                self.prepare(self._status)
            except Exception:
                log.exception("service menu: couldn't get the status ready")
        threading.Thread(target=run, name="service-prepare", daemon=True).start()

    # --- presses while it's open ------------------------------------------------------------

    def press(self, index: int) -> None:
        """Button index 0-3 while the menu is open."""
        n = index + 1
        state = self.state
        log.info("service menu (%s): button %d", state, n)
        if state == "menu":
            if n == 1:
                self._close()
                self._do("restart", RESTARTING)
            elif n == 2:
                self._close()
                self.say(WIFI)
                self._do("wifi", None)
            elif n == 3:
                self._to("rollback?", self._status or f"{self.actions['status']()} {ROLLBACK_ASK}")
                self._prepare(ROLLBACK)
            else:
                self._to("reset?", RESET_ASK)
        elif state == "rollback?":
            self._close()
            if n == 3:
                self._do("rollback", ROLLBACK, lambda ok: ok or self.say(NO_ROLLBACK))
            else:
                self.say(CANCELLED)
        elif state == "reset?":
            if n == 2:
                self._to("reset2", RESET_NEXT)
                self._prepare(RESETTING)
            else:
                self._close()
                self.say(CANCELLED)
        elif state == "reset2":
            self._close()
            if n == 3:
                self._do("reset", RESETTING)
            else:
                self.say(CANCELLED)

    def _prepare(self, text: str) -> None:
        threading.Thread(target=self.prepare, args=(text,), name="service-prepare", daemon=True).start()

    def _do(self, name: str, words: str | None, then: Callable | None = None) -> None:
        """An action, in the background (they wait for their words to be said)."""
        def run():
            try:
                result = self.actions[name]() if words is None else self.actions[name](words)
            except Exception:
                log.exception("service menu: %s failed", name)
                result = None
            if then is not None:
                then(result)
        self.worker = threading.Thread(target=run, name=f"service-{name}", daemon=True)
        self.worker.start()

    # --- the state and its time-out -------------------------------------------------------

    def _to(self, state: str, text: str) -> None:
        with self._lock:
            self.state = state
            self._deadline = self.clock() + self.wait_s
            if self._timer is None:
                self._tick()
        self.say(text)

    def _tick(self) -> None:
        self._timer = threading.Timer(min(TICK_S, self.wait_s), self._time_out)
        self._timer.daemon = True
        self._timer.start()

    def _close(self) -> None:
        with self._lock:
            self.state = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _time_out(self) -> None:
        with self._lock:
            if self.state is None:
                self._timer = None
                return
            if self.busy():                    # still talking: the wait starts when it's done
                self._deadline = self.clock() + self.wait_s
            if self.clock() < self._deadline:
                self._tick()
                return
            self.state, self._timer = None, None
        log.info("service menu: closed (no press)")
        self.say(CLOSED)


def status_text(addresses: list, wifi: dict, version: str, free_gb: float | None, songs: int,
                spoken_ip: Callable[[str], str]) -> str:
    """The status report's words."""
    parts = ["Status report."]
    if wifi.get("mode") == "hotspot":
        spot = wifi.get("hotspot") or {}
        parts.append(f"I'm on my own network, {spot.get('ssid', 'SleepRadio-Setup')}.")
    elif wifi.get("ssid"):
        parts.append(f"I'm connected to the Wi-Fi network {wifi['ssid']}.")
    elif wifi.get("mode") == "connecting":
        parts.append("I'm trying to reconnect to the Wi-Fi.")
    if addresses:
        parts.append(f"My address is {spoken_ip(addresses[0][1])}.")
    else:
        parts.append("I'm not connected to a network.")
    parts.append(f"The software is version {version}.")
    parts.append(f"There are {songs} songs" + (f", and {free_gb:.0f} gigabytes free." if free_gb is not None else "."))
    return " ".join(parts)
