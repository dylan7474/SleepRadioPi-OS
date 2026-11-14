"""Even out the volume of a stream: a continuous two-sided compressor (a "leveller" in broadcast terms) for internet
stations, podcasts, audiobooks and receivers, on top of the slow Leveller (playback/radio.py).

The Leveller sets a stream's overall level over several seconds; this works within a stream, all the time, like a
guitar compressor: the level is measured every STEP samples (~6 ms), and above TARGET the sound is turned down by the
ratio, below it turned up by the ratio (at most `lift`, and less and less as the sound nears the floor, so silence and
hiss aren't brought up). The gain comes down fast (the attack) and goes back up slowly (the release), so loud things are
caught at once and nothing "pumps"; each step's gain is reached by a straight line from the last one, so the gain never
jumps (no zipper noise). A limiter keeps the peaks under CEILING, from the first sample of a burst. It runs after the Leveller, so the level coming in is
known (pcm.LOUDNESS_TARGET_RMS, ~-19 dBFS) and the settings mean the same on every station.

MODE is the setting (stream_evenness): "off", "gentle" or "strong", read by each Evener on every block, so a change is
heard at once. In numpy: a few dozen steps a block, well under 1% of a core on a PC.
"""
from __future__ import annotations

import math

import numpy as np

from sleepradiopi.audio import pcm

STEP = 256
CEILING = 0.89               # -1 dBFS
MODES = ("off", "gentle", "strong")
PRESETS = {                  # target dBFS, ratio, attack ms, release ms, most lift dB, floor dBFS
    "gentle": (-21.0, 2.0, 20.0, 1500.0, 9.0, -50.0),
    "strong": (-21.0, 4.0, 10.0, 1000.0, 15.0, -55.0),
}
MODE = "off"


def mode(value) -> str:
    """A setting -> one of MODES (ValueError if it isn't)."""
    if value not in MODES:
        raise ValueError(f"evenness is one of {', '.join(MODES)}")
    return value


class Compressor:
    """float32 (n, channels), full scale 1.0, in and out."""

    def __init__(self, target_db: float, ratio: float, attack_ms: float, release_ms: float, lift_db: float, floor_db: float,
                 rate: int = pcm.SAMPLE_RATE) -> None:
        self.target, self.ratio, self.lift, self.floor = target_db, ratio, lift_db, floor_db
        step_s = STEP / rate
        self.a_att = math.exp(-step_s / (attack_ms / 1000))
        self.a_rel = math.exp(-step_s / (release_ms / 1000))
        self.a_lim = math.exp(-step_s / 0.05)
        self.g_db = 0.0             # the gain now, dB (down above the target, up below it)
        self.lim = 1.0              # the limiter's gain now
        self.last = 1.0             # the last step's whole gain (the ramp starts from it)

    def curve(self, level_db: np.ndarray) -> np.ndarray:
        """The gain (dB) wanted for a level (dBFS)."""
        g = np.minimum((level_db - self.target) * (1 / self.ratio - 1), self.lift)
        fade = np.clip((level_db - self.floor) / 10.0, 0.0, 1.0)
        return np.where(g > 0, g * fade, g)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        steps = -(-n // STEP)
        pad = np.zeros((steps * STEP, x.shape[1]), np.float32)
        pad[:n] = x
        blocks = pad.reshape(steps, STEP, x.shape[1])
        if n % STEP:                                            # (the last step is part padding: measure only what is there)
            ms = np.mean(blocks * blocks, axis=(1, 2))
            ms[-1] = float(np.mean(x[(steps - 1) * STEP:] ** 2))
        else:
            ms = np.mean(blocks * blocks, axis=(1, 2))
        want = self.curve(10 * np.log10(ms + 1e-10))
        peaks = np.max(np.abs(blocks), axis=(1, 2))
        gains = np.empty(steps, np.float32)
        g_db, lim = self.g_db, self.lim
        for i in range(steps):
            g_db = want[i] + (g_db - want[i]) * (self.a_att if want[i] < g_db else self.a_rel)
            g = 10 ** (g_db / 20)
            need = CEILING / (peaks[i] * g) if peaks[i] * g > CEILING else 1.0
            lim = need if need < lim else need + (lim - need) * self.a_lim
            gains[i] = g * lim
        self.g_db, self.lim = g_db, lim
        t = np.arange(1, STEP + 1, dtype=np.float32) / STEP
        start = np.concatenate(([self.last], gains[:-1])).astype(np.float32)
        ramp = (start[:, None] + (gains - start)[:, None] * t[None, :]).reshape(-1)
        self.last = float(gains[-1])
        cap = np.repeat(np.where(peaks > 0, CEILING / np.maximum(peaks, 1e-9), np.inf), STEP)   # (the limiter, from each step's
        return np.clip(x * np.minimum(ramp, cap)[:n, None], -1.0, 1.0)                           # first sample: a burst never clips)


class Evener:
    """One stream's: int16 blocks in and out, as MODE says now (a fresh compressor when it changes)."""

    def __init__(self) -> None:
        self.mode = "off"
        self.comp: Compressor | None = None

    def process(self, block: np.ndarray) -> np.ndarray:
        if MODE != self.mode:
            self.mode = MODE
            self.comp = Compressor(*PRESETS[MODE]) if MODE in PRESETS else None
        if self.comp is None or len(block) == 0:
            return block
        y = self.comp.process(block.astype(np.float32) / 32768.0)
        return np.clip(np.rint(y * 32767.0), -32768, 32767).astype(np.int16)
