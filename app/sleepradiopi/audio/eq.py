"""A 3-band EQ (bass, mid, treble) for the Pi's own speakers.

The MiniAmp is a plain DAC with no controls, so the EQ is done here, in
numpy (the image has no scipy). The three bands are the usual RBJ biquads --
a low shelf, a peak and a high shelf -- but instead of running them sample by
sample (far too slow in Python on a Zero 2 W) their combined response is
turned into one linear-phase FIR filter and applied block by block with an
FFT (overlap-save). A new setting only rebuilds the filter; the next block
is played through both the old and the new filter and crossfaded, because
swapping a long filter mid-stream clicks.

An optional low cut (a 4th-order Butterworth high-pass, 24 dB/octave) keeps
the lowest bass out of the speakers. With the box's bass port it goes just
below the port's note (~140 Hz for 160 Hz): below that a ported box stops
holding the cones back, and the bass EQ would only make them flap.

The filter delays the speaker by TAPS // 2 samples (~23 ms), always, even
when flat (then it's only a delay, no FFTs), so switching the EQ on or off
never makes the audio jump. On a Zero 2 W a non-flat EQ costs ~9% of one
core. Boosts would clip, so the filter is lowered by the largest boost (turn
the volume up instead).
"""

from __future__ import annotations

import math
import threading

import numpy as np

TAPS = 2047                 # odd, so the delay is a whole number of samples
NFFT = 4096                 # holds TAPS - 1 frames of history + a block of up to 2050
MAX_DB = 12
MAX_HIGHPASS_HZ = 300

# The bass shelf sits at 200 Hz, not the usual ~100: the box's 40 mm drivers
# resonate at ~250 Hz and its vent props them up only down to ~150 Hz, so
# there's next to nothing below that to boost (a 120 Hz shelf mostly pushed
# the cones at notes they can't play). Keep web/analyser in step.
BANDS = {                   # name: (kind, frequency Hz)
    "bass": ("lowshelf", 200.0),
    "mid": ("peak", 1000.0),
    "treble": ("highshelf", 6000.0),
}


def _biquad(kind: str, f0: float, db: float, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """RBJ Audio EQ Cookbook coefficients (b, a); shelves with S = 1, peak Q = 0.7."""
    A = 10 ** (db / 40)
    w0 = 2 * math.pi * f0 / rate
    cw, sw = math.cos(w0), math.sin(w0)
    if kind == "peak":
        alpha = sw / (2 * 0.7)
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    else:
        alpha = sw / 2 * math.sqrt(2)          # shelf slope S = 1
        k = 2 * math.sqrt(A) * alpha
        if kind == "lowshelf":
            b = [A * ((A + 1) - (A - 1) * cw + k), 2 * A * ((A - 1) - (A + 1) * cw),
                 A * ((A + 1) - (A - 1) * cw - k)]
            a = [(A + 1) + (A - 1) * cw + k, -2 * ((A - 1) + (A + 1) * cw),
                 (A + 1) + (A - 1) * cw - k]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + k), -2 * A * ((A - 1) + (A + 1) * cw),
                 A * ((A + 1) + (A - 1) * cw - k)]
            a = [(A + 1) - (A - 1) * cw + k, 2 * ((A - 1) - (A + 1) * cw),
                 (A + 1) - (A - 1) * cw - k]
    return np.array(b), np.array(a)


def response(gains: dict, rate: int, freqs: np.ndarray, highpass: float = 0) -> np.ndarray:
    """The EQ's gain (linear magnitude) at each frequency, before headroom."""
    z1 = np.exp(-2j * np.pi * freqs / rate)          # z^-1 on the unit circle
    mag = np.ones(len(freqs))
    for name, (kind, f0) in BANDS.items():
        db = gains.get(name, 0)
        if db:
            b, a = _biquad(kind, f0, db, rate)
            mag *= np.abs((b[0] + b[1] * z1 + b[2] * z1 ** 2) / (a[0] + a[1] * z1 + a[2] * z1 ** 2))
    if highpass:
        with np.errstate(divide="ignore"):
            mag *= 1 / np.sqrt(1 + (highpass / freqs) ** 8)   # 0 at DC
    return mag


def clamp(gains: dict) -> dict:
    return {name: max(-MAX_DB, min(MAX_DB, int(round(gains.get(name, 0))))) for name in BANDS}


class Equalizer:
    """process() takes and returns float32 blocks shaped (frames, channels)."""

    def __init__(self, rate: int, channels: int, gains: dict | None = None,
                 highpass: float = 0) -> None:
        self.rate = rate
        self._history = np.zeros((TAPS - 1, channels), dtype=np.float32)
        self._lock = threading.Lock()
        self._spectrum = self._playing = None      # None = flat
        self.highpass = 0
        self.set(gains or {}, highpass)
        self._playing = self._spectrum             # nothing to fade from at the start

    def set(self, gains: dict, highpass: float | None = None) -> None:
        """New band gains (dB) and, if given, the low cut in Hz (0 = off)."""
        gains = clamp(gains)
        if highpass is None:
            highpass = self.highpass
        highpass = int(max(0, min(MAX_HIGHPASS_HZ, highpass)))
        spectrum = None
        if any(gains.values()) or highpass:
            freqs = np.fft.rfftfreq(NFFT, 1 / self.rate)
            mag = response(gains, self.rate, freqs, highpass)
            mag /= max(1.0, mag.max())                 # headroom for the biggest boost
            h = np.fft.irfft(mag, NFFT)                # zero phase: centred on sample 0
            h = np.roll(h, TAPS // 2)[:TAPS] * np.hanning(TAPS)
            spectrum = np.fft.rfft(h, NFFT)[:, None]
        with self._lock:
            self.gains = gains
            self.highpass = highpass
            self._spectrum = spectrum

    def reset(self) -> None:
        """Forget the audio in the filter, e.g. when the speaker is paused."""
        with self._lock:
            self._history[:] = 0

    @staticmethod
    def _filter(spectrum, buf: np.ndarray, n: int) -> np.ndarray:
        if spectrum is None:                           # the same as a delta at TAPS // 2
            start = TAPS - 1 - TAPS // 2
            return buf[start:start + n]
        y = np.fft.irfft(np.fft.rfft(buf, NFFT, axis=0) * spectrum, NFFT, axis=0)
        return y[TAPS - 1:TAPS - 1 + n]                # the part the wrap-around can't reach

    def process(self, block: np.ndarray) -> np.ndarray:
        n = len(block)
        if n > NFFT - TAPS + 1:
            return np.concatenate([self.process(block[i:i + 1024]) for i in range(0, n, 1024)])
        with self._lock:
            buf = np.concatenate([self._history, block.astype(np.float32)])
            self._history = buf[n:]
            new = self._spectrum
            out = self._filter(new, buf, n)
            if new is not self._playing:
                old = self._filter(self._playing, buf, n)
                fade = np.linspace(0, 1, n, endpoint=False, dtype=np.float32)[:, None]
                out = old + (out - old) * fade
                self._playing = new
        return out.astype(np.float32)
