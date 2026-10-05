"""Noise reduction for receivers: the steady hiss under a voice, turned down.

A receiver's audio -- short wave through someone's KiwiSDR, a weak FM
channel on your own -- is a voice in steady noise. Here the sound is taken
apart into narrow bands of frequency, the noise in each is learnt from the
quietest moments of the last few seconds (speech comes and goes; the hiss
stays), and each band is turned down by how little it stands above that: a
Wiener gain, with the decision-directed smoothing that keeps it from
warbling. No band goes below a floor: some hiss is left, because the
artefacts live down there and dead silence between words sounds wrong.

It works on what's about to be played (the speaker's rate), so it's the same
however the receiver's audio arrived. It suits speech in noise: on a clean
broadcast station there's nothing for it to take away but the programme's
own quiet parts.

LEVEL is the setting ("off", "light", "strong"): main.py and the web side
set it, and whoever plays a receiver looks at it block by block.
"""

from __future__ import annotations

import numpy as np

from sleepradiopi.audio import pcm

LEVELS = {  # how far a band of nothing but noise is turned down (dB), and how much the noise is over-estimated
    "light": (-9.0, 1.2),
    "strong": (-18.0, 1.8),
}
LEVEL = "off"
FRAME = 1024                 # samples a frame (23 ms at the speaker's rate), half of it new each time
HISTORY_S = 2.5              # the noise is the least each band has been in this long
LOOK_S = 0.2                 # ...looked at afresh this often


def clean_level(level) -> str:
    level = str(level or "off").lower()
    if level != "off" and level not in LEVELS:
        raise ValueError("noise reduction is off, light or strong")
    return level


class Denoiser:
    """process(): blocks of the speaker's PCM in, the same out (half a frame late, in whole half-frames:
    a call can give back a little more or less than it was given). One voice: the first channel is what's
    cleaned, and every channel gets it -- a receiver is mono."""

    def __init__(self, level: str = "light", rate: int = pcm.SAMPLE_RATE) -> None:
        self.level = level
        floor_db, self.over = LEVELS[level]
        self.floor = 10 ** (floor_db / 20)
        self.n, self.hop = FRAME, FRAME // 2
        self.win = np.sqrt(np.hanning(self.n + 1)[:-1]).astype(np.float32)   # (twice over, at half a frame: sums to one)
        bins = self.n // 2 + 1
        self.every = max(1, int(LOOK_S * rate / self.hop))
        self.hist = np.zeros((max(1, int(HISTORY_S * rate / self.hop / self.every)), bins), dtype=np.float32)
        self.have = 0
        self.least = np.full(bins, np.inf, dtype=np.float32)   # the least each band has been in this stretch
        self.count = 0
        self.noise: np.ndarray | None = None
        self.smooth: np.ndarray | None = None                  # each band's power, smoothed a little in time
        self.prev = np.zeros(bins, dtype=np.float32)           # the last frame's cleaned power over the noise
        self.tail = np.zeros(self.hop, dtype=np.float32)       # the last half-frame in, and the half-frame out still to finish
        self.out = np.zeros(self.hop, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)

    def process(self, block: np.ndarray) -> np.ndarray:
        channels = block.shape[1] if block.ndim == 2 else 1
        mono = block[:, 0] if block.ndim == 2 else block
        self.pending = np.concatenate([self.pending, mono.astype(np.float32)])
        done = []
        while len(self.pending) >= self.hop:
            new, self.pending = self.pending[:self.hop], self.pending[self.hop:]
            frame = np.concatenate([self.tail, new])
            self.tail = new
            spec = np.fft.rfft(frame * self.win)
            power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
            self.smooth = power if self.smooth is None else 0.7 * self.smooth + 0.3 * power
            self.least = np.minimum(self.least, self.smooth)
            self.count += 1
            if self.count >= self.every or self.noise is None:
                if self.count >= self.every:
                    self.hist = np.roll(self.hist, 1, axis=0)
                    self.hist[0] = self.least
                    self.have = min(self.have + 1, len(self.hist))
                    self.least = np.full_like(self.least, np.inf)
                    self.count = 0
                least = self.hist[:self.have].min(axis=0) if self.have else self.smooth
                self.noise = least * 2.0 + 1e-3                # (a minimum sits under the average: brought back up)
            gamma = power / (self.noise * self.over)
            xi = 0.94 * self.prev + 0.06 * np.maximum(gamma - 1.0, 0.0)
            gain = np.maximum(xi / (1.0 + xi), self.floor)
            gain[1:-1] = (gain[:-2] + 2 * gain[1:-1] + gain[2:]) / 4      # (neighbouring bands move together: fewer chirps)
            self.prev = (gain ** 2) * gamma
            y = np.fft.irfft(spec * gain, self.n).astype(np.float32) * self.win
            done.append(self.out + y[:self.hop])
            self.out = y[self.hop:]
        if not done:
            return np.zeros((0, channels) if block.ndim == 2 else (0,), dtype=np.int16)
        clean = np.clip(np.rint(np.concatenate(done)), -32768, 32767).astype(np.int16)
        return np.repeat(clean[:, None], channels, axis=1) if block.ndim == 2 else clean


class Switch:
    """The setting, as whoever plays a receiver uses it: process() cleans the block at LEVEL, or hands it
    back as it is when that's off (a change of level starts afresh)."""

    def __init__(self) -> None:
        self._denoiser: Denoiser | None = None

    def process(self, block: np.ndarray) -> np.ndarray:
        level = LEVEL
        if level not in LEVELS:
            self._denoiser = None
            return block
        if self._denoiser is None or self._denoiser.level != level:
            self._denoiser = Denoiser(level)
        return self._denoiser.process(block)
