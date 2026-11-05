"""A 10-band graphic EQ (31 Hz to 16 kHz) for the Pi's own speakers.

The MiniAmp is a plain DAC with no controls, so the EQ is done here, in
numpy (the image has no scipy). The ten bands are the usual RBJ peaking
biquads, but instead of running them sample by
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

# The EQ is ten octave-spaced peaks (Q 1.4), 31 Hz to 16 kHz. All of them go
# into the one FIR filter, so ten cost the same CPU as one. The box's 40 mm
# drivers resonate at ~250 Hz and its vent props them up only down to ~150 Hz,
# so the lowest two bands do little. Keep web/analyser in step.
GRAPHIC = ("g31", "g62", "g125", "g250", "g500", "g1k", "g2k", "g4k", "g8k", "g16k")
BANDS = {}                  # name: (kind, frequency Hz, Q for a peak)
for _name in GRAPHIC:
    _hz = _name[1:]
    BANDS[_name] = ("peak", float(_hz[:-1]) * 1000 if _hz.endswith("k") else float(_hz), 1.4)

# The old bass / mid / treble controls are gone, but settings saved by an older
# radio, the analyser's suggestion and old backups still use them: each one
# sets the graphic bands it used to cover (name: {band: share of the dB}).
LEGACY = {
    "bass": {"g31": 1, "g62": 1, "g125": 1, "g250": 0.5},
    "mid": {"g500": 0.6, "g1k": 1, "g2k": 0.6},
    "treble": {"g4k": 0.5, "g8k": 1, "g16k": 1},
}


# Ready-made sounds, shared by the page and the buttons' EQ mode (web/desktop.html
# keeps a copy for its buttons; tests/test_eq.py checks they agree).
PRESETS = {                 # name: gain in dB for each of GRAPHIC
    "Flat":       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Speech":     [0, 0, -3, -2, 0, 2, 4, 3, 0, -2],
    "Warm":       [0, 2, 3, 2, 1, 0, -1, -2, -3, -4],
    "Bright":     [0, 0, 0, 0, 0, 1, 2, 4, 5, 4],
    "Late night": [0, 0, -2, -1, 0, 0, -1, -3, -5, -6],
    "Rock":       [0, 2, 4, 2, -1, -2, 0, 3, 4, 4],
    "Pop":        [0, 0, 2, 3, 1, 0, 1, 2, 3, 2],
    "Classical":  [0, 0, 0, 0, 0, 0, 0, -1, 2, 3],
    "Jazz":       [0, 0, 2, 2, 1, 0, 0, 1, 2, 2],
    "Acoustic":   [0, 0, 1, 2, 2, 1, 1, 2, 3, 2],
    "Dance":      [0, 2, 4, 3, 0, -1, 0, 2, 3, 2],
}
SLOTS = 4                                   # the box's buttons
DEFAULT_SLOTS = ["Speech", "Warm", "Rock", "Late night"]


def preset_bands(name: str) -> dict:
    return dict(zip(GRAPHIC, PRESETS[name]))


def validate_slots(slots) -> list:
    """What each button picks in EQ mode: a sound's name (a ready-made one, or one
    saved on the page) or None (nothing)."""
    if not isinstance(slots, list) or len(slots) != SLOTS:
        raise ValueError(f"EQ mode has {SLOTS} buttons: send a list of {SLOTS} sound names (or null)")
    out = []
    for s in slots:
        if s is not None and (not isinstance(s, str) or len(s) > 40):
            raise ValueError("a button's sound is a name (up to 40 letters), or null")
        out.append(" ".join(s.split()) or None if isinstance(s, str) else None)
    return out


def known(name: str) -> bool:
    return name in BANDS or name in LEGACY


def expand(gains: dict) -> dict:
    """Gains with any old bass/mid/treble turned into graphic bands (a graphic
    band given in the same dict wins)."""
    out = {}
    for name, db in gains.items():
        for band, share in LEGACY.get(name, {}).items():
            out[band] = db * share
    out.update({k: v for k, v in gains.items() if k in BANDS})
    return out


def _biquad(kind: str, f0: float, db: float, rate: int, q: float = 0.7) -> tuple[np.ndarray, np.ndarray]:
    """RBJ Audio EQ Cookbook coefficients (b, a); shelves with S = 1, peak Q = q."""
    A = 10 ** (db / 40)
    w0 = 2 * math.pi * f0 / rate
    cw, sw = math.cos(w0), math.sin(w0)
    if kind == "peak":
        alpha = sw / (2 * q)
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
    for name, (kind, f0, q) in BANDS.items():
        db = gains.get(name, 0)
        if db:
            b, a = _biquad(kind, f0, db, rate, q)
            mag *= np.abs((b[0] + b[1] * z1 + b[2] * z1 ** 2) / (a[0] + a[1] * z1 + a[2] * z1 ** 2))
    if highpass:
        with np.errstate(divide="ignore"):
            mag *= 1 / np.sqrt(1 + (highpass / freqs) ** 8)   # 0 at DC
    return mag


def clamp(gains: dict) -> dict:
    gains = expand(gains)
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


MAX_SOUNDS = 20


def validate_sounds(sounds) -> list[dict]:
    """Saved speaker set-ups (the desktop's Speaker sounds folder) -> clean copies:
    [{"name", "eq": {"g31".."g16k"}, "highpass": Hz, "mono": bool}]."""
    if not isinstance(sounds, list) or len(sounds) > MAX_SOUNDS:
        raise ValueError(f"send a list of up to {MAX_SOUNDS} sounds")
    out, names = [], set()
    for s in sounds:
        if not isinstance(s, dict):
            raise ValueError("a sound is an object")
        name = " ".join(s["name"].split()) if isinstance(s.get("name"), str) else ""
        if not name or len(name) > 40:
            raise ValueError("a sound needs a name (up to 40 letters)")
        if name.lower() in names:
            raise ValueError(f"there's a sound called {name!r} already")
        names.add(name.lower())
        eq = s.get("eq") or {}
        if not isinstance(eq, dict) or not all(known(k) and isinstance(v, (int, float)) and not isinstance(v, bool)
                                               and -MAX_DB <= v <= MAX_DB for k, v in eq.items()):
            raise ValueError(f"{name}: eq is band names in dB (-{MAX_DB} to {MAX_DB})")
        hz = s.get("highpass", 0)
        if isinstance(hz, bool) or not isinstance(hz, int) or not 0 <= hz <= 300:
            raise ValueError(f"{name}: highpass is 0-300 Hz")
        if not isinstance(s.get("mono", False), bool):
            raise ValueError(f"{name}: mono is true or false")
        out.append({"name": name, "eq": clamp(eq), "highpass": hz, "mono": s.get("mono", False)})
    return out
