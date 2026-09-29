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

import numpy as np

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
# The cathedral: a rotary selector and a button on the back. Turn to choose (it
# says what's there), press the back button to confirm.
SEL_MENU = ("Service menu. Turn the selector to choose, and press the button on the back to confirm. "
            "One: restart. Two: reset the Wi-Fi. Three: a status report. Four: factory reset.")
SEL_OPTIONS = ("Restart the radio.", "Reset the Wi-Fi.", "Status report.", "Factory reset.")
SEL_NOTHING = "Nothing here. Turn to one, two, three or four."
SEL_ROLLBACK_ASK = ("To go back to the previous version of the software, press the back button again. "
                    "Or turn the selector, to leave.")
SEL_RESET_ASK = ("Factory reset. This puts every setting back to how it came, including the Wi-Fi and the web "
                 "page's password. Your music, audiobooks and voices are kept. To confirm, press the back button "
                 "again. Or turn the selector, to leave.")
FIXED = (MENU, CLOSED, RESTARTING, WIFI, ROLLBACK, NO_ROLLBACK, RESET_ASK, RESET_NEXT, RESETTING, CANCELLED,
         SEL_MENU, *SEL_OPTIONS, SEL_NOTHING, SEL_RESET_ASK)


class ServiceMenu:
    """say(text) speaks (a beep first; the DJ's voice made in the background);
    prepare(text) makes a line ahead of time. actions: "wifi", "status" (->
    the words), and the ones that end with a restart -- "restart", "rollback"
    (-> True if it's happening), "reset" -- which get their words: they go
    quiet at once, say them, and only then act (no music in between)."""

    def __init__(self, say: Callable[[str], None], actions: dict, prepare: Callable[[str], None] = lambda t: None,
                 wait_s: float = WAIT_S, clock=time.monotonic, busy: Callable[[], bool] = lambda: False,
                 on_open: Callable[[], None] = lambda: None,
                 on_close: Callable[[bool], None] = lambda resume: None) -> None:
        """busy(): still making or saying the words (the wait starts after).
        on_open(): the menu opens (pause the show); on_close(resume): it
        closed -- resume the show (once its last words are said), unless the
        radio is about to restart."""
        self.say, self.prepare, self.actions, self.busy = say, prepare, actions, busy
        self.selector = False                 # the cathedral: select() + confirm() instead of press()
        self.choice: int | None = None
        self.on_open, self.on_close = on_open, on_close
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
        if self.state is None:
            self.on_open()
        self.choice = None
        self._to("menu", SEL_MENU if self.selector else MENU)
        if self._status is None:           # (opened from the page: no hold to make them in)
            self._prepare_all()

    def _prepare_all(self) -> None:
        """Make the menu's words and the status report now (slow on a Zero), so
        a press speaks at once."""
        def run():
            self.prepare(SEL_MENU if self.selector else MENU)
            self.prepare(RESTARTING)
            try:
                self._status = f"{self.actions['status']()} {self.rollback_ask}"
                self.prepare(self._status)
            except Exception:
                log.exception("service menu: couldn't get the status ready")
        threading.Thread(target=run, name="service-prepare", daemon=True).start()

    @property
    def rollback_ask(self) -> str:
        return SEL_ROLLBACK_ASK if self.selector else ROLLBACK_ASK

    # --- the selector and the back button (the cathedral) -----------------------------------

    def select(self, index: int) -> None:
        """The selector reached a position while the menu is open: say what's there
        (or, at a yes/no question, leave it)."""
        state = self.state
        log.info("service menu (%s): selector %d", state, index + 1)
        if state == "menu":
            if index < len(SEL_OPTIONS):
                self.choice = index
                self._to("menu", SEL_OPTIONS[index])
            else:
                self.choice = None
                self._to("menu", SEL_NOTHING)
        elif state in ("rollback?", "reset?"):
            self.say(CANCELLED)
            self._close()

    def confirm(self) -> None:
        """The back button while the menu is open: do what's chosen / say yes."""
        state = self.state
        log.info("service menu (%s): confirm %s", state, self.choice)
        if state == "menu":
            if self.choice is None:
                self._to("menu", SEL_MENU)
            elif self.choice == 0:
                self._close(resume=False)
                self._do("restart", RESTARTING)
            elif self.choice == 1:
                self.say(WIFI)
                self._close()
                self._do("wifi", None)
            elif self.choice == 2:
                self._to("rollback?", self._status or f"{self.actions['status']()} {SEL_ROLLBACK_ASK}")
                self._prepare(ROLLBACK)
            else:
                self._to("reset?", SEL_RESET_ASK)
                self._prepare(RESETTING)
        elif state == "rollback?":
            self._close(resume=False)
            self._do("rollback", ROLLBACK, lambda ok: ok or (self.say(NO_ROLLBACK), self.on_close(True)))
        elif state == "reset?":
            self._close(resume=False)
            self._do("reset", RESETTING)

    # --- presses while it's open ------------------------------------------------------------

    def press(self, index: int) -> None:
        """Button index 0-3 while the menu is open."""
        n = index + 1
        state = self.state
        log.info("service menu (%s): button %d", state, n)
        if state == "menu":
            if n == 1:
                self._close(resume=False)
                self._do("restart", RESTARTING)
            elif n == 2:
                self.say(WIFI)
                self._close()
                self._do("wifi", None)
            elif n == 3:
                self._to("rollback?", self._status or f"{self.actions['status']()} {ROLLBACK_ASK}")
                self._prepare(ROLLBACK)
            else:
                self._to("reset?", RESET_ASK)
        elif state == "rollback?":
            if n == 3:
                self._close(resume=False)
                self._do("rollback", ROLLBACK, lambda ok: ok or (self.say(NO_ROLLBACK), self.on_close(True)))
            else:
                self.say(CANCELLED)
                self._close()
        elif state == "reset?":
            if n == 2:
                self._to("reset2", RESET_NEXT)
                self._prepare(RESETTING)
            else:
                self.say(CANCELLED)
                self._close()
        elif state == "reset2":
            if n == 3:
                self._close(resume=False)
                self._do("reset", RESETTING)
            else:
                self.say(CANCELLED)
                self._close()

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

    def _close(self, resume: bool = True) -> None:
        with self._lock:
            was, self.state = self.state, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if was is not None:
            self.on_close(resume)

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
        self.on_close(True)


class MenuSound:
    """What the speaker plays while the menu is open (the show is paused):
    its words as they're ready, silence between, and a soft tick once a
    second while words are still being made -- never a dead silence."""

    label = "Service menu"
    done = False

    def __init__(self, tick: np.ndarray, rate: int = 44100, channels: int = 2, tick_s: float = 1.0) -> None:
        self.tick, self.rate, self.channels = tick, rate, channels
        self.every = int(tick_s * rate)
        self._lock = threading.Lock()
        self._queue: list[np.ndarray] = []
        self._pos = 0                          # into _queue[0]
        self.making = 0                        # words being made
        self._t = 0

    def put(self, audio: np.ndarray) -> None:
        """Say this now (cutting off whatever was being said)."""
        with self._lock:
            self._queue, self._pos = [audio], 0

    def add(self, audio: np.ndarray) -> None:
        with self._lock:
            self._queue.append(audio)

    @property
    def talking(self) -> bool:
        with self._lock:
            return bool(self._queue) or self.making > 0

    def next(self, n: int) -> np.ndarray:
        out = np.zeros((n, self.channels), np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._queue:
                part = self._queue[0][self._pos:self._pos + n - filled]
                out[filled:filled + len(part)] = part
                filled += len(part)
                self._pos += len(part)
                if self._pos >= len(self._queue[0]):
                    self._queue.pop(0)
                    self._pos = 0
            ticking = not filled and self.making > 0
        if ticking:                            # waiting for words: tick, once a second
            k = (self._t + np.arange(n)) % self.every
            at = k < len(self.tick)
            out[at] = self.tick[k[at]]
        self._t += n
        return out


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
