"""Evening out a stream's volume (audio/compressor.py): the two-sided compressor after the Leveller."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import compressor, pcm
from sleepradiopi.playback.radio import Leveller

RATE = pcm.SAMPLE_RATE


def tone(seconds: float, level_db: float, f: float = 440.0) -> np.ndarray:
    """A stereo sine whose RMS is level_db dBFS, int16."""
    t = np.arange(int(seconds * RATE)) / RATE
    x = np.sin(2 * np.pi * f * t) * (10 ** (level_db / 20) * np.sqrt(2))
    return np.clip(np.rint(np.repeat(x[:, None], 2, axis=1) * 32767), -32768, 32767).astype(np.int16)


def run(x: np.ndarray, mode: str, monkeypatch, level: bool = True) -> np.ndarray:
    """Through the station's chain (the Leveller, then the evener) in 1024-frame blocks."""
    monkeypatch.setattr(compressor, "MODE", mode)
    lev, ev = Leveller(), compressor.Evener()
    return np.concatenate([ev.process(lev.process(x[i:i + 1024]) if level else x[i:i + 1024]) for i in range(0, len(x), 1024)])


def db(x: np.ndarray) -> float:
    y = x.astype(np.float64) / 32768
    return 10 * np.log10(np.mean(y * y) + 1e-12)


def test_off_changes_nothing(monkeypatch) -> None:
    x = tone(2, -30)
    assert np.array_equal(run(x, "off", monkeypatch, level=False), x)


@pytest.mark.parametrize("mode, most_gap", [("gentle", 7.0), ("strong", 4.0)])
def test_a_quiet_stretch_and_a_loud_one_end_up_closer(monkeypatch, mode, most_gap) -> None:
    # 20 s quiet then 20 s 18 dB louder: the Leveller alone (slow, at most +6 dB) leaves most of the gap; evened out, much less
    x = np.concatenate([tone(20, -36), tone(20, -18)])
    off, on = run(x, "off", monkeypatch), run(x, mode, monkeypatch)
    sec = RATE
    gap = lambda y: db(y[25 * sec:40 * sec]) - db(y[5 * sec:20 * sec])
    assert gap(off) > 9.0
    assert abs(gap(on)) < most_gap and abs(gap(on)) < gap(off) - 4


def test_a_sudden_loud_burst_is_caught_at_once_and_peaks_stay_under_the_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(compressor, "MODE", "strong")
    ev = compressor.Evener()
    quiet, burst = tone(5, -30), tone(1, -3)
    for i in range(0, len(quiet), 1024):
        ev.process(quiet[i:i + 1024])
    out = np.concatenate([ev.process(burst[i:i + 1024]) for i in range(0, len(burst), 1024)])
    assert np.abs(out).max() <= compressor.CEILING * 32767 + 2         # (the limiter: -1 dBFS)
    assert db(out[RATE // 10:]) < -10                                   # (held down within a tenth of a second)


def test_silence_and_hiss_are_not_brought_up(monkeypatch) -> None:
    for mode in ("gentle", "strong"):
        hiss = tone(10, -65)
        out = run(hiss, mode, monkeypatch, level=False)
        assert db(out[5 * RATE:]) < -63                                 # (below the floor: left alone)


def test_the_gain_never_jumps(monkeypatch) -> None:
    monkeypatch.setattr(compressor, "MODE", "strong")
    ev = compressor.Evener()
    x = np.concatenate([tone(3, -40), tone(3, -10)])
    y = np.concatenate([ev.process(x[i:i + 1024]) for i in range(0, len(x), 1024)]).astype(np.float64)
    ratio = np.abs(y[:, 0]) / (np.abs(x[:, 0].astype(np.float64)) + 1)
    for part in (slice(RATE // 2, 3 * RATE - 2 * compressor.STEP), slice(3 * RATE + RATE // 5, 6 * RATE)):   # (each steady stretch, and the loud one after its clamp-down; the step holding the burst's start comes down early)
        r, xs = ratio[part], np.abs(x[part, 0])
        peaks = r[xs > xs.max() * 0.95]                                 # (the gain at each wave's crest: it only glides)
        assert np.abs(np.diff(peaks)).max() < 0.05 * peaks.mean()


def test_a_change_of_mode_is_heard_at_once_and_bad_modes_are_refused(monkeypatch) -> None:
    monkeypatch.setattr(compressor, "MODE", "off")
    ev = compressor.Evener()
    x = tone(1, -40)
    assert np.array_equal(ev.process(x[:1024]), x[:1024])
    monkeypatch.setattr(compressor, "MODE", "strong")
    assert not np.array_equal(ev.process(x[:1024]), x[:1024])
    for bad in ("loud", None, 1):
        with pytest.raises(ValueError):
            compressor.mode(bad)
    assert ev.process(np.zeros((0, 2), np.int16)).shape == (0, 2)


def test_it_is_cheap(monkeypatch) -> None:
    import time
    monkeypatch.setattr(compressor, "MODE", "strong")
    ev = compressor.Evener()
    x = tone(30, -20)
    t = time.perf_counter()
    for i in range(0, len(x), 1024):
        ev.process(x[i:i + 1024])
    assert time.perf_counter() - t < 3.0                                # (30 s of sound in well under 10% of the time, even on a slow PC)


def test_the_setting_from_the_web_and_in_the_config(tmp_path: Path, monkeypatch) -> None:
    from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
    from sleepradiopi.config.settings import load
    monkeypatch.setattr(compressor, "MODE", "off")
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    control = SpeakerControl(SpeakerOutput(), lambda: None, lambda: None, config_file=conf)
    control.set_evenness("gentle")
    assert compressor.MODE == "gentle" and json.loads(conf.read_text())["stream_evenness"] == "gentle" and load(conf).stream_evenness == "gentle"
    assert control.status()["evenness"] == "gentle"
    with pytest.raises(ValueError):
        control.set_evenness("max")
    assert compressor.MODE == "gentle"
