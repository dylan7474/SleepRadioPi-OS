"""Procedural coloured noise generator.

Reference: SleepRadio's NoiseGenerator.kt (white/pink/brown/blue/deep-space/
ambient colour presets). Port the filter math; replace AudioTrack output
with a numpy buffer fed to a sounddevice stream.
"""

from enum import Enum, auto


class NoiseColor(Enum):
    WHITE = auto()
    PINK = auto()
    BROWN = auto()
    BLUE = auto()
    DEEP_SPACE = auto()
    AMBIENT = auto()


class NoiseGenerator:
    """Generates a stream of coloured-noise samples for the ambient mixer."""

    def __init__(self, sample_rate: int = 44_100) -> None:
        self.sample_rate = sample_rate
        self.color = NoiseColor.PINK

    def set_color(self, color: NoiseColor) -> None:
        self.color = color

    def next_block(self, n_frames: int):
        """Return the next n_frames of noise samples as a numpy array.

        TODO: port the per-colour filter coefficients from NoiseGenerator.kt.
        """
        raise NotImplementedError
