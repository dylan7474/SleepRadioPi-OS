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
    """Tells a short press (on release) from a long one (fires while held)."""

    def __init__(self, on_short: Callable[[], None], on_long: Callable[[], None],
                 long_s: float = LONG_PRESS_S, name: str = "knob") -> None:
        self.on_short, self.on_long, self.long_s, self.name = on_short, on_long, long_s, name
        self._timer: threading.Timer | str | None = None
        self._lock = threading.Lock()

    def down(self) -> None:
        with self._lock:
            if self._timer is not None:
                return
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
        if isinstance(timer, threading.Timer):
            timer.cancel()
            self.on_short()


class Knob:
    def __init__(self, on_turn: Callable[[int], None], on_press: Callable[[], None],
                 devices: Path = Path("/dev/input"),
                 on_long_press: Callable[[], None] | None = None,
                 buttons: dict | None = None) -> None:
        """buttons: keycode -> (on_short, on_long) for the preset buttons."""
        self.on_turn = on_turn
        self.keys = {}
        for code, (short, long_) in (buttons or {}).items():
            t = PressTimer(short, long_, name=f"button {code - 1}")
            self.keys[code] = (t.down, t.up)
        if on_long_press is None:            # act as soon as it's pressed
            self.on_press, self.on_release = on_press, None
        else:
            timer = PressTimer(on_press, on_long_press)
            self.on_press, self.on_release = timer.down, timer.up
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
