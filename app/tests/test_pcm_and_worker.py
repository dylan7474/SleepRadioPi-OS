import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.tts.worker import clauses


def test_edge_trim_matches_trackprobe_rules():
    assert pcm.edge_trim(200_000, 1_000, 190_000) == (950, 190_150)
    assert pcm.edge_trim(200_000, 100, 199_900) == (0, 0)       # both edges too short to trim
    assert pcm.edge_trim(200_000, 6_000, 150_000) == (0, 0)     # lead too long, tail too long
    assert pcm.edge_trim(5_000, 4_000, 3_000) == (0, 0)         # start after end collapses


def test_sound_bounds():
    quiet, loud = 1e-8, 0.01
    env = np.array([quiet, quiet, loud, loud, quiet])
    assert pcm.sound_bounds(env) == (40, 80)
    assert pcm.sound_bounds(np.array([quiet, quiet])) == (-1, -1)


def test_gain_is_clamped():
    assert pcm.rms_to_gain(0.11) == 1.0
    assert pcm.rms_to_gain(0.001) == pcm.MAX_ITEM_GAIN
    assert pcm.rms_to_gain(1.0) == pcm.MIN_ITEM_GAIN
    assert pcm.rms_to_gain(0.0) == 1.0


def test_soft_limit_compresses_instead_of_clipping():
    out = pcm.apply_gain(np.array([30_000, -30_000, 1_000], dtype=np.int16), 2.0)
    assert out[2] == 2_000
    assert 32_000 < out[0] <= 32_767 and -32_768 <= out[1] < -32_000


def test_clauses_split_with_pauses():
    parts = clauses("That was Pink Moon, by Nick Drake. Coming up, Money.")
    assert [p for p, _ in parts] == ["That was Pink Moon,", "by Nick Drake.", "Coming up,", "Money."]
    assert [pause for _, pause in parts] == [0.12, 0.30, 0.12, 0.0]
