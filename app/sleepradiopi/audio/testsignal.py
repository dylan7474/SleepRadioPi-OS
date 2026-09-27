"""Test sounds for the Pi's own speakers, e.g. to compare two back panels.

They replace the show on the speaker while they play (the show carries on
underneath, and the browser stream isn't touched), at the speaker's volume
but with the EQ and low cut bypassed, so what you hear is the box itself.

  bass   a slow sine sweep, 40-600 Hz (where a bass port makes its difference)
  sweep  a sine sweep across the whole range, 40 Hz-16 kHz
  pink   pink noise (equal energy per octave), for a phone spectrum-analyser
         app: play it with each panel and compare the curves
  left / right   pink noise on one speaker only: is each one working, and
         on the correct side?
  phase  low pink noise that switches every 3 s between both speakers the
         same ("in phase") and the right one inverted ("out of phase"). With
         the speakers wired correctly, in phase sounds fuller and centred; if
         out of phase sounds fuller, one speaker's wires are swapped (its +
         and - at the amp), which also makes centred vocals vanish in stereo

The sweeps are logarithmic, so every octave takes the same time, and the
page shows the frequency playing now. Everything starts and ends with a
short fade, so there are no clicks.
"""

from __future__ import annotations

import math

import numpy as np

LEVEL = 10 ** (-12 / 20)     # peak -12 dBFS: easy on 40 mm speakers at a high volume
FADE_S = 0.05
NOISE_LOOP_S = 4             # noise made by FFT repeats seamlessly; keeps RAM low on a Zero

PHASE_STEP_S = 3.0

KINDS = {                    # name: (label, duration s, f0, f1); f0 None = noise
    "bass": ("Bass sweep", 24.0, 40.0, 600.0),
    "sweep": ("Full sweep", 24.0, 40.0, 16000.0),
    "pink": ("Pink noise", 15.0, None, None),
    "left": ("Left speaker", 6.0, None, None),
    "right": ("Right speaker", 6.0, None, None),
    "phase": ("Phase check", 18.0, None, None),
}


def _pink(n: int, rate: int, seed: int = 7, f_hi: float = 20000) -> np.ndarray:
    """Pink noise, 20 Hz-f_hi, peak-normalised to 1."""
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / rate)
    shape = np.zeros_like(f)
    band = (f >= 20) & (f <= f_hi)
    shape[band] = 1 / np.sqrt(f[band])
    x = np.fft.irfft(spectrum * shape, n)
    return x / np.max(np.abs(x))


class TestSignal:
    __test__ = False             # not a pytest test class

    def __init__(self, kind: str, rate: int, channels: int) -> None:
        self.kind = kind
        self.label, self.duration_s, self.f0, self.f1 = KINDS[kind]
        self.rate, self.channels = rate, channels
        self.total = int(self.duration_s * rate)
        self.pos = 0
        # The phase check uses low noise only: that's where polarity is easiest to hear.
        self._noise = None if self.f0 is not None else \
            _pink(int(NOISE_LOOP_S * rate), rate, f_hi=500 if kind == "phase" else 20000)

    @property
    def done(self) -> bool:
        return self.pos >= self.total

    @property
    def elapsed_s(self) -> float:
        return min(self.pos, self.total) / self.rate

    def hz(self) -> float | None:
        """The sweep's frequency now (None for noise)."""
        if self.f0 is None:
            return None
        return self.f0 * (self.f1 / self.f0) ** (self.elapsed_s / self.duration_s)

    def note(self) -> str | None:
        """What's playing now, for the phase check."""
        if self.kind != "phase":
            return None
        if int(self.elapsed_s // PHASE_STEP_S) % 2 == 0:
            return "In phase (both the same)"
        return "Out of phase (right inverted)"

    def next(self, n: int) -> np.ndarray:
        """The next n frames as int16-scaled float32, (n, channels); silence after the end."""
        i = np.arange(self.pos, self.pos + n)
        live = i < self.total
        t = i / self.rate
        if self.f0 is None:
            x = np.zeros(n)
            x[live] = self._noise[i[live] % len(self._noise)]
        else:
            k = math.log(self.f1 / self.f0)
            phase = 2 * np.pi * self.f0 * self.duration_s / k * (np.exp(t * k / self.duration_s) - 1)
            x = np.sin(phase) * live
        if self.kind == "phase":                   # fade in and out of every step
            ts = t % PHASE_STEP_S
            fade = np.clip(np.minimum(ts, PHASE_STEP_S - ts) / FADE_S, 0, 1)
        else:
            fade = np.clip(np.minimum(t, self.duration_s - t) / FADE_S, 0, 1)
        self.pos += n
        y = (x * fade * LEVEL * 32767).astype(np.float32)
        out = np.repeat(y[:, None], self.channels, axis=1)
        if self.kind == "left":
            out[:, 1:] = 0
        elif self.kind == "right":
            out[:, 0] = 0
        elif self.kind == "phase":
            flip = (t // PHASE_STEP_S).astype(int) % 2 == 1
            out[flip, 1] *= -1
        return out
