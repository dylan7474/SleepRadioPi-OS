"""Coloured noise for sleeping to, layered on top of whatever the radio plays.

The colours are SleepRadio's (NoiseGenerator.kt): white, pink, brown, deep
brown (its "deep space"), blue, violet, and ambient (pink with a slow
swell). The app filtered sample by sample; that's far too slow in Python on
a Zero, so here each colour is made by shaping the spectrum of Gaussian
noise with an FFT, a block at a time, and the blocks are overlapped with a
power-complementary (sine) window so the joins can't be heard and it never
repeats. Every colour comes out at the same loudness as the show's music
(pcm.LOUDNESS_TARGET_RMS), so an even balance means an even mix.
"""

from __future__ import annotations

import math

import numpy as np

from sleepradiopi.audio import pcm

KINDS = {                     # name: (label, what it sounds like)
    "white": ("White", "Even hiss, like a detuned radio"),
    "pink": ("Pink", "Softer, like steady rain"),
    "brown": ("Brown", "Deep, like a waterfall or distant surf"),
    "deep": ("Deep brown", "Deeper still, a low rumble"),
    "blue": ("Blue", "Bright, like a gentle spray"),
    "violet": ("Violet", "Brighter, very high hiss"),
    "ambient": ("Ambient", "Pink noise with a slow, gentle swell"),
}
BLOCK = 32768                 # ~0.74 s per FFT block; blocks overlap by half
SWELL_HZ = 0.1                # ambient: one slow rise and fall every 10 s


def _shape(kind: str, rate: int, n: int) -> np.ndarray:
    """Amplitude per FFT bin for a colour (power slope: pink 1/f, brown 1/f^2, ...)."""
    f = np.fft.rfftfreq(n, 1 / rate)
    f[0] = f[1]
    corner = 25.0 if kind == "deep" else 40.0            # below this, no more boost (no DC rumble)
    fc = np.maximum(f, corner)
    slope = {"white": 0.0, "pink": -0.5, "ambient": -0.5, "brown": -1.0, "deep": -1.2,
             "blue": 0.5, "violet": 1.0}[kind]
    a = (fc / 1000.0) ** slope
    a[f < 15] = 0.0                                       # nothing the speakers could play
    return a


class NoiseGen:
    """An endless stream of one colour, stereo (the two sides independent, so it
    sounds wide), in the station's units (int16 scale, as float32)."""

    def __init__(self, kind: str = "pink", rate: int = pcm.SAMPLE_RATE, channels: int = pcm.CHANNELS,
                 seed: int | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"no such noise: {kind}")
        self.kind, self.rate, self.channels = kind, rate, channels
        self.rng = np.random.default_rng(seed)
        self.shape = _shape(kind, rate, BLOCK)
        self.window = np.sin(np.pi * (np.arange(BLOCK) + 0.5) / BLOCK).astype(np.float32)[:, None]
        # scale so the colour comes out at the music's loudness
        power = np.sum(self.shape ** 2) * 2 / BLOCK ** 2      # mean square of irfft(shaped unit noise)
        self.scale = pcm.LOUDNESS_TARGET_RMS * 32767.0 / math.sqrt(max(power, 1e-12))
        self._tail = np.zeros((BLOCK // 2, channels), np.float32)
        self._ready = np.zeros((0, channels), np.float32)
        self._t = 0

    def _block(self) -> np.ndarray:
        spec = self.rng.standard_normal((BLOCK // 2 + 1, self.channels)) \
            + 1j * self.rng.standard_normal((BLOCK // 2 + 1, self.channels))
        x = np.fft.irfft(spec * self.shape[:, None] / math.sqrt(2), n=BLOCK, axis=0).astype(np.float32)
        return x * self.scale * self.window

    def next(self, n: int) -> np.ndarray:
        while len(self._ready) < n:
            b = self._block()
            half = BLOCK // 2
            self._ready = np.concatenate([self._ready, self._tail + b[:half]])
            self._tail = b[half:]
        out, self._ready = self._ready[:n], self._ready[n:]
        if self.kind == "ambient":
            t = (self._t + np.arange(n)) / self.rate
            swell = (0.8 + 0.2 * np.sin(2 * np.pi * SWELL_HZ * t)) / 0.8124   # (as loud on average)
            out = out * swell.astype(np.float32)[:, None]
        self._t += n
        return out
