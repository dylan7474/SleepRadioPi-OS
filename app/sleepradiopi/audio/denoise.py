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

The third level, "rig", is the chain a modern amateur transceiver has, for
a voice and nothing else: the passband narrowed to the voice (250 to 2900 Hz,
with a little lift above 1.5 kHz, where the consonants are), an automatic
notch (a whistle that stays put for seconds is
taken out), and a noise reduction that first judges, band by band, whether
speech is there at all (OM-LSA: Cohen's speech-presence weighting of the
log-spectral-amplitude gain) -- which is what lets it turn the hiss down as
far as "strong" does while leaving more of the voice. Measured with a clean
voice in real band noise it keeps 1 to 2 dB more of the voice than "strong"
for the same hiss. It isn't for music or broadcast FM: the passband alone
sees to that. (A rig's noise blanker isn't here: tried, it took nothing off
a click that the passband hadn't already -- a blanker works on the signal
before it's narrowed, which is the receiver's end, not ours.)

LEVEL is the setting ("off", "light", "strong", "rig"): main.py and the web
side set it, and whoever plays a receiver looks at it block by block.
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


RIG = "rig"
RIG_BAND = (250.0, 2900.0)   # the voice: what's let through
RIG_FLOOR_DB = -22.0         # a band with no speech in it: this far down
RIG_LIFT_DB = 3.0            # above 1.5 kHz: a little up, for clarity
NOTCH_OVER = 25.0            # a narrow line this far (14 dB) over what's either side of it, for seconds: a whistle


def clean_level(level) -> str:
    level = str(level or "off").lower()
    if level != "off" and level != RIG and level not in LEVELS:
        raise ValueError("noise reduction is off, light, strong or rig")
    return level


def _lsa_table() -> tuple[np.ndarray, np.ndarray]:
    """exp(E1(v) / 2) for v from 0.001 to 100 (the log-spectral-amplitude gain's second factor), worked out
    here rather than with scipy: E1(v) is the integral of exp(-v e^s) over s from 0 up."""
    v = np.logspace(-3, 2, 300)
    s = np.linspace(0.0, 14.0, 5600)
    e1 = np.trapezoid(np.exp(-np.outer(v, np.exp(s))), s, axis=1)
    return v, np.exp(0.5 * e1)


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


class Rig:
    """The "rig" level: process() as Denoiser's. See the top of the file for what it does."""

    level = RIG
    _table: tuple[np.ndarray, np.ndarray] | None = None

    def __init__(self, rate: int = pcm.SAMPLE_RATE) -> None:
        if Rig._table is None:
            Rig._table = _lsa_table()
        self.rate = rate
        self.n, self.hop = FRAME, FRAME // 2
        self.win = np.sqrt(np.hanning(self.n + 1)[:-1]).astype(np.float32)
        f = np.fft.rfftfreq(self.n, 1 / rate)
        lo, hi = RIG_BAND
        shape = np.clip((f - lo * 0.7) / (lo * 0.3), 0, 1) * np.clip((hi * 1.12 - f) / (hi * 0.12), 0, 1)
        shape *= 1 + (10 ** (RIG_LIFT_DB / 20) - 1) * np.clip((f - 1200) / 600, 0, 1)
        inside = np.flatnonzero(shape > 0)
        self.a, self.b = int(inside[0]), int(inside[-1]) + 1          # the bins the voice is in: all that's worked on
        self.shape = shape[self.a:self.b].astype(np.float32)
        bins = self.b - self.a
        self.gmin = 10 ** (RIG_FLOOR_DB / 20)
        self.every = max(1, int(LOOK_S * rate / self.hop))
        self.hist = np.zeros((max(1, int(HISTORY_S * rate / self.hop / self.every)), bins), dtype=np.float32)
        self.have = 0
        self.least = np.full(bins, np.inf, dtype=np.float32)
        self.count = 0
        self.noise: np.ndarray | None = None
        self.smooth: np.ndarray | None = None
        self.long: np.ndarray | None = None                    # the spectrum over seconds: what stays put in it is a whistle
        self.tone: np.ndarray | None = None
        self.prev = np.ones(bins, dtype=np.float32)
        self.tail = np.zeros(self.hop, dtype=np.float32)
        self.out = np.zeros(self.hop, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)

    def process(self, block: np.ndarray) -> np.ndarray:
        channels = block.shape[1] if block.ndim == 2 else 1
        mono = block[:, 0] if block.ndim == 2 else block
        self.pending = np.concatenate([self.pending, mono.astype(np.float32)])
        table_v, table_g = Rig._table
        done = []
        while len(self.pending) >= self.hop:
            new, self.pending = self.pending[:self.hop], self.pending[self.hop:]
            frame = np.concatenate([self.tail, new])
            self.tail = new
            whole = np.fft.rfft(frame * self.win)
            spec = whole[self.a:self.b]
            power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
            self.smooth = power if self.smooth is None else 0.7 * self.smooth + 0.3 * power
            self.long = power.copy() if self.long is None else 0.995 * self.long + 0.005 * power
            self.least = np.minimum(self.least, self.smooth)
            self.count += 1
            if self.count >= self.every or self.noise is None:
                if self.count >= self.every:
                    self.hist = np.roll(self.hist, 1, axis=0)
                    self.hist[0] = self.least
                    self.have = min(self.have + 1, len(self.hist))
                    self.least = np.full_like(self.least, np.inf)
                    self.count = 0
                    if self.have >= 6:                         # the notch: looked for afresh each time
                        # a whistle is one narrow line: far over the bands a little to either side of it (a voice's
                        # own peaks are broad: what's 150 Hz away is nearly as strong)
                        k = 10
                        beside = np.lib.stride_tricks.sliding_window_view(np.pad(self.long, k, mode="reflect"), 2 * k + 1)
                        around = np.median(np.concatenate([beside[:, :k - 2], beside[:, k + 3:]], axis=1), axis=1)
                        tone = self.long > NOTCH_OVER * around
                        self.tone = (np.convolve(tone, np.ones(3), "same") > 0) if tone.any() else None
                least = self.hist[:self.have].min(axis=0) if self.have else self.smooth
                self.noise = least * 2.0 + 1e-3
            gamma = np.minimum(power / self.noise, 1000.0)
            xi = np.maximum(0.92 * self.prev + 0.08 * np.maximum(gamma - 1.0, 0.0), 10 ** -2.5)
            v = np.maximum(gamma * xi / (1.0 + xi), 1e-3)
            g_lsa = np.minimum(xi / (1.0 + xi) * np.interp(v, table_v, table_g), 1.0)
            # is speech here at all? (how far the a priori SNR, averaged over the bands around, stands above nothing)
            near = np.clip((10 * np.log10(np.convolve(xi, np.ones(3) / 3, "same")) + 10) / 15, 0, 1)
            wide = np.clip((10 * np.log10(np.convolve(xi, np.ones(31) / 31, "same")) + 10) / 15, 0, 1)
            absent = np.minimum(1 - near * wide, 0.95)
            p = 1.0 / (1.0 + absent / (1 - absent) * (1 + xi) * np.exp(-v))
            gain = (g_lsa ** p) * (self.gmin ** (1 - p))
            self.prev = (g_lsa ** 2) * gamma
            if self.tone is not None:
                gain = np.where(self.tone, np.minimum(gain, 0.05), gain)
            kept = np.zeros_like(whole)                        # (nothing outside the voice's band)
            kept[self.a:self.b] = spec * (gain * self.shape)
            y = np.fft.irfft(kept, self.n).astype(np.float32) * self.win
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
        self._denoiser: Denoiser | Rig | None = None

    def process(self, block: np.ndarray) -> np.ndarray:
        level = LEVEL
        if level != RIG and level not in LEVELS:
            self._denoiser = None
            return block
        if self._denoiser is None or self._denoiser.level != level:
            self._denoiser = Rig() if level == RIG else Denoiser(level)
        return self._denoiser.process(block)
