"""FT8 and FT4 read off a receiver's audio, as the messages sent.

FT8 and FT4 are the amateur bands' weak-signal modes: every station sends a
short fixed message (a call, an answer, a report) in a slot of its own, FT8's
15 seconds long and FT4's 7.5, all of them starting on the clock. A receiver
left on a band's FT8 frequency in upper sideband hears some dozens of them
at once, each a warble about 50 Hz wide, and a slot's worth of sound reads
back as a list: who called whom, how strong, from where.

How it's read:

  * The sound a receiver plays is kept, with the time by the clock at which
    it came (broadcast/station.py feeds it, as it does the Morse reader).
  * When a slot has ended its sound is cut out, brought to the 12 kHz the
    decoder wants, and given to it as a WAV file.
  * The decoding is ft8_lib's (github.com/kgoba/ft8_lib, MIT): its
    decode_ft8 program, in the image as /usr/bin/decode_ft8. It runs as a
    process of its own at low priority, so it never takes the processor from
    the thread playing the sound; a slot takes it a fraction of a second.

The clock matters and the sound is late: a slot is cut where the clock says,
a second on (CUT_LATE_S). Measured on 20 m against the same sound cut at
other moments: the decoder reads as much with the cut anywhere from a second
and a half early to two seconds late, so a second on takes sound that
arrives anything up to two and a half seconds after it was sent. A radio
whose clock is seconds out reads nothing: the time comes from the network
(and the clock chip between times).

Receive only: nothing is sent, here or anywhere.

`reader` is the one the radio uses: the station feeds it, the web side
switches it and asks what it has read.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from typing import Callable

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

MODES = {"ft8": 15.0, "ft4": 7.5}    # a slot, in seconds
OUT_RATE = 12_000                    # what the decoder takes
DECIMATE = 3                         # of the speaker's rate: 14700 samples a second holds a receiver's 3 kHz with room to spare
RATE = pcm.SAMPLE_RATE / DECIMATE
CUT_LATE_S = 1.0             # a slot is cut this long after the clock's (see the top)
WAIT_S = 0.3                 # ...and looked at this long after it has all come
GAP_S = 1.0                  # sound this far from when it was due: the receiver stopped and started
FOLLOW = 0.05                # (how fast the clock is followed, a block)
ENOUGH = 0.9                 # of a slot's sound, for it to be worth reading
KEEP_S = 2 * max(MODES.values()) + 5
STEP_S = 0.25
DECODE_TIMEOUT_S = 12.0      # (a slot's reading that takes longer than this is given up)
DECODER = "decode_ft8"       # ft8_lib's; SLEEPRADIO_FT8_DECODER names another
LINE = re.compile(r"^\d{6}\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+(\d+)\s+~\s+(\S.*?)\s*$")


def decoder_path() -> str | None:
    """The decoding program, or None: this radio can't read these."""
    named = os.environ.get("SLEEPRADIO_FT8_DECODER")
    if named:
        return named if os.access(named, os.X_OK) else None
    return shutil.which(DECODER)


def _taps(rate: float, cutoff: float, n: int = 63) -> np.ndarray:
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / rate * k) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


_LOW_PASS = _taps(RATE, 4500.0)


def to_decoder_rate(x: np.ndarray) -> np.ndarray:
    """Sound at RATE -> at OUT_RATE, as 16-bit samples."""
    if not len(x):
        return np.zeros(0, np.int16)
    y = np.convolve(x, _LOW_PASS, mode="same")
    n = int(len(x) * OUT_RATE / RATE)
    y = np.interp(np.arange(n) * (RATE / OUT_RATE), np.arange(len(y)), y)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


def parse(out: str) -> list[dict]:
    """What decode_ft8 prints -> [{"snr" (dB), "dt" (s into the slot), "hz" (in the sound), "text"}], strongest first."""
    found = []
    for line in out.splitlines():
        m = LINE.match(line)
        if m:
            found.append({"snr": float(m.group(1)), "dt": float(m.group(2)), "hz": int(m.group(3)), "text": " ".join(m.group(4).split())})
    return sorted(found, key=lambda d: -d["snr"])


def run_decoder(samples: np.ndarray, mode: str) -> list[dict]:
    """A slot of sound at OUT_RATE -> the messages in it. RuntimeError says why not."""
    program = decoder_path()
    if program is None:
        raise RuntimeError("no decoder on this radio")
    with tempfile.TemporaryDirectory(prefix="ft8-") as tmp:
        path = os.path.join(tmp, "slot.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(samples.astype("<i2").tobytes())
        cmd = [program, *(["-ft4"] if mode == "ft4" else []), path]
        nice = shutil.which("nice")
        try:                                         # (the sound comes first)
            done = subprocess.run([nice, "-n", "10", *cmd] if nice else cmd, capture_output=True, text=True, timeout=DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError("the decoder took too long") from None
        except OSError as e:
            raise RuntimeError(f"the decoder wouldn't start: {e}") from None
    return parse(done.stdout)


class Reader:
    """Hears a receiver's sound and reads the FT8 (or FT4) in it, a slot at a time: feed() it blocks as
    they're played, step() it a few times a second, ask status() what it has read."""

    def __init__(self, clock: Callable[[], float] = time.time, decode: Callable[[np.ndarray, str], list[dict]] = run_decoder) -> None:
        self.on = False
        self.mode = "ft8"
        self._clock, self._decode = clock, decode
        self._lock = threading.Lock()
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._x = np.zeros(0, np.float32)            # the sound kept, at RATE
        self._end = 0.0                              # ...and when its last sample came, by the clock
        self._next: float | None = None              # the start of the next slot to read
        self._n = 0
        self._messages: deque[dict] = deque(maxlen=400)
        self._slots = 0
        self._last: dict | None = None               # the slot last looked at: {"t", "heard" | "error"}
        self._turn = 0                               # (which switching-on this is: a slot read for the last one is dropped)

    # --- the sound coming in -----------------------------------------------------------------

    def feed(self, block: np.ndarray) -> None:
        """What's being played (a block at the speaker's rate): kept, with when it came. Cheap."""
        if not self.on:
            return
        now = self._clock()
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        with self._lock:
            mono = np.concatenate((self._left, mono)) if len(self._left) else mono
            whole = len(mono) // DECIMATE * DECIMATE
            self._left = mono[whole:]
            if whole:
                # When it came: by the count of samples since the last, drawn gently to the clock (blocks come in
                # fits and starts; the samples don't). A gap is a new start: what was kept is from another time.
                due = self._end + whole / pcm.SAMPLE_RATE
                if len(self._x) and abs(now - due) < GAP_S:
                    self._end = due + FOLLOW * (now - due)
                else:
                    self._x, self._end = np.zeros(0, np.float32), now
                self._x = np.concatenate((self._x, mono[:whole].reshape(-1, DECIMATE).mean(axis=1)))[-int(KEEP_S * RATE):]

    # --- what the page asks for --------------------------------------------------------------

    def status(self, since: int = 0) -> dict:
        """{"on", "mode", "can" (there's a decoder), "slot" (s), "slots" (read so far), "last": {"t", "heard" | "error"},
        "messages": [{"n", "t" (the slot's start, by the clock), "snr", "dt", "hz", "text"}] after number since}"""
        with self._lock:
            return {"on": self.on, "mode": self.mode, "can": decoder_path() is not None, "slot": MODES[self.mode],
                    "slots": self._slots, "last": self._last, "messages": [m for m in self._messages if m["n"] > since]}

    # --- the reading -------------------------------------------------------------------------

    def step(self) -> None:
        """Read the slot that has just ended, if one has and its sound is all here."""
        slot = MODES[self.mode]
        now = self._clock()
        if self._next is None:                       # (just switched on: from the next slot to begin)
            self._next = math.ceil(now / slot) * slot
        t0 = self._next
        if now < t0 + CUT_LATE_S + slot + WAIT_S:
            return
        self._next = t0 + slot
        if now > t0 + CUT_LATE_S + 2 * slot:         # (the radio was held up: that one's gone, on from now)
            self._next = math.ceil(now / slot) * slot - slot
            return
        with self._lock:
            x, end, turn = self._x, self._end, self._turn
        a = len(x) - int(round((end - (t0 + CUT_LATE_S)) * RATE))     # where the slot begins in what's kept
        b = a + int(slot * RATE)
        got = x[max(a, 0):max(min(b, len(x)), 0)]
        if len(got) < ENOUGH * slot * RATE:          # (nothing playing for part of it)
            return
        if a < 0:
            got = np.concatenate((np.zeros(-a, np.float32), got))
        try:
            found = self._decode(to_decoder_rate(got), self.mode)
        except RuntimeError as e:
            with self._lock:
                if turn == self._turn:
                    self._last = {"t": t0, "error": str(e)}
            return
        with self._lock:
            if turn != self._turn:
                return
            self._slots += 1
            self._last = {"t": t0, "heard": len(found)}
            for m in found:
                self._n += 1
                self._messages.append({"n": self._n, "t": t0, **m})


class Live(Reader):
    """The Reader as the radio uses it: stepped by a thread of its own while it's on."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._switching = threading.Lock()
        self._stop = threading.Event()

    def switch(self, on: bool, mode: str | None = None) -> None:
        """On or off, FT8 or FT4. On (or another mode) starts from nothing. ValueError: no such mode."""
        on, mode = bool(on), (mode or self.mode)
        if mode not in MODES:
            raise ValueError("mode is ft8 or ft4")
        with self._switching:
            if on == self.on and mode == self.mode:
                return
            self._stop.set()
            with self._lock:
                self.on, self.mode = on, mode
                self._left, self._x = np.zeros(0, np.float32), np.zeros(0, np.float32)
                self._next, self._last = None, None
                self._turn += 1
                if on:
                    self._messages.clear()
                    self._slots = 0
            if on:
                self._stop = stop = threading.Event()
                threading.Thread(target=self._run, args=(stop,), name="ft8-reader", daemon=True).start()

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(STEP_S):
            try:
                self.step()
            except Exception:                        # (a reading that falls over mustn't end the reader)
                log.exception("ft8: reading failed")


reader = Live()
