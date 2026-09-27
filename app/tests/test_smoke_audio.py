"""Tests the pure tone-generation math in scripts/smoke_test_audio.py.

Does NOT test actual playback -- that needs sounddevice + real hardware
(the Pi + HiFiBerry). This just confirms generate_tone() produces sane
samples, so a bug there doesn't waste a trip to the Pi to discover.
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

# scripts/ isn't a package (it's deploy/debug tooling, not app code), so
# load the module directly by path rather than via a normal import.
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "smoke_test_audio.py"
_spec = importlib.util.spec_from_file_location("smoke_test_audio", _SCRIPT_PATH)
smoke_test_audio = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke_test_audio
_spec.loader.exec_module(smoke_test_audio)

generate_tone = smoke_test_audio.generate_tone


def test_generate_tone_shape_and_dtype() -> None:
    tone = generate_tone(freq_hz=440.0, seconds=1.0, sample_rate=44_100)
    assert tone.dtype == np.float32
    assert tone.shape == (44_100,)


def test_generate_tone_stays_within_volume() -> None:
    tone = generate_tone(freq_hz=220.0, seconds=0.5, sample_rate=8_000, volume=0.3)
    assert np.max(np.abs(tone)) <= 0.3 + 1e-6


def test_generate_tone_rejects_bad_volume() -> None:
    with pytest.raises(ValueError):
        generate_tone(freq_hz=440.0, seconds=0.1, sample_rate=8_000, volume=0.0)
    with pytest.raises(ValueError):
        generate_tone(freq_hz=440.0, seconds=0.1, sample_rate=8_000, volume=1.5)
