"""Binaural beat generator.

Reference: SleepRadio's BinauralGenerator.kt (a set of named beat-frequency
presets, e.g. delta/theta/alpha bands). Port the two-tone generation math;
output feeds the same ambient mixer as noise.py.
"""

from enum import Enum, auto


class BinauralPreset(Enum):
    OFF = auto()
    # TODO: mirror the exact preset set from BinauralGenerator.kt / BinauralPreset.kt


class BinauralGenerator:
    """Generates left/right binaural-beat tones for the ambient mixer."""

    def __init__(self, sample_rate: int = 44_100) -> None:
        self.sample_rate = sample_rate
        self.preset = BinauralPreset.OFF

    def set_preset(self, preset: BinauralPreset) -> None:
        self.preset = preset

    def next_block(self, n_frames: int):
        """Return the next n_frames of (left, right) samples.

        TODO: port the base-frequency + beat-offset math from
        BinauralGenerator.kt.
        """
        raise NotImplementedError
