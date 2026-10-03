"""The volume knob: a rotary encoder with a push switch.

The kernel does the GPIO work (config.txt on the Pi):
  dtoverlay=rotary-encoder,pin_a=17,pin_b=27,relative_axis=1
  dtoverlay=gpio-key,gpio=22,keycode=164,label="PLAYPAUSE"
Both show up as /dev/input/event* devices, read here without extra
libraries: each click of the knob is a relative-axis event (+1/-1) and the
push is a key press. Any relative axis or key works, so a different encoder
or button needs no code change -- except the preset buttons' keys (KEY_1 to
KEY_4, io/presets.py), which go to their own button.

With a long-press action, a press acts when it's let go (pause/play), and
holding it for LONG_PRESS_S does the long-press action instead (say the
radio's address).

Holding two preset buttons together (a chord: 1 and 4 open the service
menu, io/service.py) cancels both buttons' own press and hold the moment
the second goes down, so neither plays anything; held for
the chord's time, its action runs.
"""

from __future__ import annotations

import logging
import os
import select
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# struct input_event: struct timeval (two longs), __u16 type, __u16 code, __s32 value
EVENT = struct.Struct("llHHi")
EV_KEY, EV_REL = 1, 2
KEY_UP, KEY_DOWN = 0, 1
LONG_PRESS_S = 3.0
PRESS_GUARD_S = 0.15  # a preset button pressed again this soon after a press is a bounce, not a press
                      # (short: a real second press steps a button on -- io/presets.py STEP_SETTLE_S)
RESCAN_S = 10.0   # look for new input devices (modules can load after we start)


def handle(data: bytes, on_turn: Callable[[int], None], on_press: Callable[[], None],
           on_release: Callable[[], None] | None = None, keys: dict | None = None) -> None:
    """Dispatch every complete event in data (key repeats are ignored). keys:
    keycode -> (on_down, on_up) for keys with a job of their own."""
    for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _, _, etype, code, value = EVENT.unpack_from(data, i)
        if etype == EV_REL and value:
            on_turn(value)
        elif etype == EV_KEY and keys and code in keys:
            if value == KEY_DOWN:
                keys[code][0]()
            elif value == KEY_UP:
                keys[code][1]()
        elif etype == EV_KEY and value == KEY_DOWN:
            on_press()
        elif etype == EV_KEY and value == KEY_UP and on_release is not None:
            on_release()


class PressTimer:
    """Tells a short press (on release) from a long one (fires while held).

    guard_s: a press that starts this soon after the last short press doesn't
    count as another short one -- a worn or loose switch can open for a few ms
    mid-press, which made two presses of one (and the second would step a
    button on past the one wanted). Holding it still counts as a hold."""

    def __init__(self, on_short: Callable[[], None], on_long: Callable[[], None],
                 long_s: float = LONG_PRESS_S, name: str = "knob", guard_s: float = 0.0) -> None:
        self.on_short, self.on_long, self.long_s, self.name = on_short, on_long, long_s, name
        self.guard_s = guard_s
        self._timer: threading.Timer | str | None = None
        self._lock = threading.Lock()
        self._last_short = -1e9          # time.monotonic() of the last short press
        self._guarded = False            # this press came too soon after the last: no short press

    def down(self) -> None:
        with self._lock:
            if self._timer is not None:
                return
            self._guarded = time.monotonic() - self._last_short < self.guard_s
            self._timer = threading.Timer(self.long_s, self._long)
            self._timer.daemon = True
            self._timer.start()

    def _long(self) -> None:
        with self._lock:
            if self._timer is None:
                return
            self._timer = "fired"     # the release that follows does nothing
        log.info("%s: long press", self.name)
        self.on_long()

    def up(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
            guarded = self._guarded
            if isinstance(timer, threading.Timer) and not guarded:
                self._last_short = time.monotonic()
        if isinstance(timer, threading.Timer):
            timer.cancel()
            if guarded:
                log.info("%s: pressed again within %.1f s: a bounce, ignored", self.name, self.guard_s)
                return
            self.on_short()

    def cancel(self) -> None:
        """Part of a chord: this press does nothing, now or when it's let go."""
        with self._lock:
            if isinstance(self._timer, threading.Timer):
                self._timer.cancel()
            if self._timer is not None:
                self._timer = "cancelled"


class Chord:
    """Keys held together: on_start when they're all down, on_fire if they
    stay down for hold_s. The keys' own PressTimers are cancelled."""

    def __init__(self, codes, hold_s: float, on_start: Callable[[], None], on_fire: Callable[[], None],
                 on_tick: Callable[[], None] | None = None, tick_s: float = 1.0) -> None:
        """on_tick: every tick_s while they're held (from the start), so you
        can hear it counting."""
        self.codes, self.hold_s = frozenset(codes), hold_s
        self.on_start, self.on_fire = on_start, on_fire
        self.on_tick, self.tick_s = on_tick, tick_s
        self.down: set = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def key(self, code: int, pressed: bool, timers: dict) -> None:
        with self._lock:
            if pressed:
                self.down.add(code)
                if len(self.down) > 1:                 # two buttons at once: neither acts
                    for c in self.down:
                        if c in timers:
                            timers[c].cancel()
                start = self.codes <= self.down and self._timer is None
                if start:
                    self._timer = threading.Timer(self.hold_s, self._fire)
                    self._timer.daemon = True
                    self._timer.start()
            else:
                self.down.discard(code)
                start = False
                if self._timer is not None and code in self.codes:
                    self._timer.cancel()
                    self._timer = None
                    log.info("chord let go early")
        if start:
            self.on_start()
            if self.on_tick is not None:
                threading.Thread(target=self._ticks, args=(self._timer,), name="chord-ticks", daemon=True).start()

    def _ticks(self, timer) -> None:
        end = time.monotonic() + self.hold_s - self.tick_s / 2
        while self._timer is timer and time.monotonic() < end:
            self.on_tick()
            time.sleep(self.tick_s)

    def _fire(self) -> None:
        with self._lock:
            if self._timer is None:
                return
            self._timer = None
        log.info("chord held")
        self.on_fire()


class Knob:
    def __init__(self, on_turn: Callable[[int], None], on_press: Callable[[], None],
                 devices: Path = Path("/dev/input"),
                 on_long_press: Callable[[], None] | None = None,
                 buttons: dict | None = None, chord=None, raw_keys: dict | None = None,
                 on_held_turn: Callable[[int], None] | None = None) -> None:
        """buttons: keycode -> (on_short, on_long[, long_s]) for the preset
        buttons; chord: a Chord (or a list of them): some of them held together;
        raw_keys: keycode -> (on_down, on_up), passed straight through (a rotary
        selector's positions); on_held_turn: the knob turned while pressed (then
        the press itself does nothing when it's let go)."""
        chords = [] if chord is None else (list(chord) if isinstance(chord, (list, tuple)) else [chord])
        self.on_turn = on_turn
        self.keys = {}
        timers = {}
        for code, spec in (buttons or {}).items():
            short, long_ = spec[0], spec[1]
            t = timers[code] = PressTimer(short, long_, long_s=spec[2] if len(spec) > 2 else LONG_PRESS_S,
                                          name=f"button {code - 1}", guard_s=PRESS_GUARD_S)
            if not chords:
                self.keys[code] = (t.down, t.up)
            else:
                self.keys[code] = (lambda t=t, c=code: (t.down(), [ch.key(c, True, timers) for ch in chords]),
                                   lambda t=t, c=code: ([ch.key(c, False, timers) for ch in chords], t.up()))
        self.keys.update(raw_keys or {})
        self._press_timer = None
        if on_long_press is None:            # act as soon as it's pressed
            self.on_press, self.on_release = on_press, None
        else:
            timer = self._press_timer = PressTimer(on_press, on_long_press)
            self.on_press, self.on_release = timer.down, timer.up
        self.held = False                    # the knob's switch is down
        if on_held_turn is not None and self._press_timer is not None:
            down, up, turn = self.on_press, self.on_release, on_turn

            def pressed() -> None:
                self.held = True
                down()

            def released() -> None:
                self.held = False
                up()

            def turned(clicks: int) -> None:
                if self.held:
                    self._press_timer.cancel()   # (let go, it neither pauses nor says the address)
                    on_held_turn(clicks)
                else:
                    turn(clicks)
            self.on_press, self.on_release, self.on_turn = pressed, released, turned
        self.devices = devices
        self._fds: dict[str, int] = {}

    def start(self) -> None:
        threading.Thread(target=self._run, name="knob", daemon=True).start()

    def _rescan(self) -> None:
        for path in sorted(self.devices.glob("event*")):
            if str(path) in self._fds:
                continue
            try:
                self._fds[str(path)] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                log.info("knob: listening to %s", path)
            except OSError as e:
                log.warning("knob: can't open %s: %s", path, e)
                self._fds[str(path)] = -1   # don't retry every rescan

    def _run(self) -> None:
        next_scan = 0.0
        while True:
            if time.monotonic() >= next_scan:
                self._rescan()
                next_scan = time.monotonic() + RESCAN_S
            fds = [fd for fd in self._fds.values() if fd >= 0]
            if not fds:
                time.sleep(RESCAN_S)
                continue
            ready, _, _ = select.select(fds, [], [], RESCAN_S)
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:   # device went away
                    data = b""
                if not data:
                    os.close(fd)
                    self._fds = {k: v for k, v in self._fds.items() if v != fd}
                    continue
                try:
                    handle(data, self.on_turn, self.on_press, self.on_release, self.keys)
                except Exception:
                    log.exception("knob: handler failed")
