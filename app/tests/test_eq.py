import json
from pathlib import Path

import numpy as np

from sleepradiopi.audio.eq import TAPS, Equalizer, response
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config.settings import load

RATE = 44_100
DELAY = TAPS // 2


def _sine(freq: float, seconds: float = 0.5) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    x = (10000 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.column_stack([x, x])


def _run(eq: Equalizer, x: np.ndarray, block: int = 1024) -> np.ndarray:
    return np.concatenate([eq.process(x[i:i + block]) for i in range(0, len(x), block)])


def _level_db(eq: Equalizer, freq: float) -> float:
    x = _sine(freq)
    y = _run(eq, x)
    steady = slice(RATE // 10, None)          # after the filter has filled
    return 20 * np.log10(np.std(y[steady, 0]) / np.std(x[steady, 0]))


def test_flat_is_the_input_delayed() -> None:
    eq = Equalizer(RATE, 2)
    x = np.random.default_rng(1).uniform(-20000, 20000, (8000, 2)).astype(np.float32)
    y = _run(eq, x)
    assert np.allclose(y[DELAY:], x[:-DELAY], atol=2)
    assert np.allclose(y[:DELAY], 0, atol=2)


def test_blocks_of_any_size_give_the_same_result() -> None:
    x = np.random.default_rng(2).uniform(-20000, 20000, (9000, 2)).astype(np.float32)
    gains = {"bass": 6, "mid": -3, "treble": 4}
    whole = _run(Equalizer(RATE, 2, gains), x, block=len(x))
    for size in (1, 100, 1024, 2050, 3000):
        assert np.allclose(_run(Equalizer(RATE, 2, gains), x, block=size), whole, atol=1)


def test_bands_shape_the_sound_with_headroom_for_boosts() -> None:
    eq = Equalizer(RATE, 2, {"bass": 12})       # lowered 12 dB so nothing clips
    assert abs(_level_db(eq, 40)) < 1           # +12 boost - 12 headroom
    assert abs(_level_db(eq, 5000) + 12) < 0.5
    eq.set({"treble": -6})
    assert abs(_level_db(eq, 16000) + 6) < 1
    assert abs(_level_db(eq, 100)) < 0.5
    eq.set({"mid": -9})
    assert abs(_level_db(eq, 1000) + 9) < 0.5
    assert abs(_level_db(eq, 60)) < 1


def test_fir_matches_the_biquads() -> None:
    gains = {"bass": 5, "mid": -4, "treble": 7}
    eq = Equalizer(RATE, 2, gains)
    for f in (80, 400, 1000, 3000, 10000):
        want = 20 * np.log10(response(gains, RATE, np.array([f]))[0]) - 7
        assert abs(_level_db(eq, f) - want) < 0.5, f


def test_speaker_plays_through_the_eq_and_resets_on_reopen(tmp_path: Path) -> None:
    out = tmp_path / "played.raw"
    spk = SpeakerOutput(command=["sh", "-c", f"cat >> {out}"], eq=Equalizer(RATE, 2))
    spk.volume = 100
    spk.start()
    spk.write(np.full((4000, 2), 30000, dtype=np.int16))
    spk.stop()
    played = np.frombuffer(out.read_bytes(), dtype=np.int16).reshape(-1, 2)
    assert (abs(played[DELAY:4000].astype(int) - 30000) <= 2).all()
    assert (abs(played[:DELAY]) <= 2).all()
    spk.start()
    assert not spk.eq._history.any()
    spk.stop()


def test_control_sets_and_saves_the_eq(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"speaker_mono": True}))
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(RATE, 2))
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    assert ctl.status()["eq"] == {"bass": 0, "mid": 0, "treble": 0}
    ctl.set_eq({"bass": 4})
    ctl.set_eq({"treble": 40})                  # clamped, and bass is kept
    assert ctl.status()["eq"] == {"bass": 4, "mid": 0, "treble": 12}
    saved = json.loads(conf.read_text())
    assert saved == {"speaker_mono": True, "speaker_eq": {"bass": 4, "mid": 0, "treble": 12}}
    assert load(conf).speaker_eq == {"bass": 4, "mid": 0, "treble": 12}


def test_switching_the_eq_doesnt_click() -> None:
    x = _sine(300, 0.3)
    eq = Equalizer(RATE, 2)
    out = []
    for i, start in enumerate(range(0, len(x), 1024)):
        eq.set({"bass": 12, "treble": -12} if i % 2 else {})   # a big change every block
        out.append(eq.process(x[start:start + 1024]))
    y = np.concatenate(out)
    # no step bigger than the sine itself makes (a click was 10x that)
    assert np.max(np.abs(np.diff(y[:, 0]))) < 1.2 * np.max(np.abs(np.diff(x[:, 0])))


def test_low_cut_rolls_off_the_deep_bass() -> None:
    eq = Equalizer(RATE, 2, highpass=140)
    assert abs(_level_db(eq, 1000)) < 0.3
    assert abs(_level_db(eq, 140) + 3) < 1          # -3 dB at the corner
    assert _level_db(eq, 70) < -20                   # 24 dB/octave below it
    assert abs(_level_db(eq, 400)) < 0.5


def test_low_cut_leaves_room_for_the_bass_boost() -> None:
    # With the low cut the bass shelf peaks lower, so less overall level is given up.
    boosted = Equalizer(RATE, 2, {"bass": 8})
    with_cut = Equalizer(RATE, 2, {"bass": 8}, highpass=140)
    assert _level_db(with_cut, 3000) > _level_db(boosted, 3000) + 1
    assert _level_db(with_cut, 60) < _level_db(boosted, 60) - 10


def test_control_sets_and_saves_the_low_cut(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(RATE, 2))
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    assert ctl.status()["highpass_hz"] == 0
    ctl.set_highpass(140)
    ctl.set_eq({"bass": 3})                          # the EQ keeps the low cut
    assert spk.eq.highpass == 140 and ctl.status()["highpass_hz"] == 140
    assert load(conf).speaker_highpass_hz == 140
    ctl.set_highpass(0)
    assert load(conf).speaker_highpass_hz == 0 and spk.eq._spectrum is not None   # bass 3 still on
