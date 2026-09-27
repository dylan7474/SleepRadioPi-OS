from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.broadcast.station import ClockStep, OnAir, Speech, Step

from test_offline import _station

BLOCK = 4410  # 0.1 s


def _done(value) -> Future:
    f = Future()
    f.set_result(value)
    return f


class SkipAt:
    """An Output that asks the station to skip after [after] blocks."""

    def __init__(self, after: int) -> None:
        self.after, self.blocks, self.station = after, 0, None

    def start(self): ...
    def stop(self): ...

    def write(self, block) -> None:
        self.blocks += 1
        if self.blocks == self.after:
            assert self.station.skip()


def _playing_station(tmp_path: Path, monkeypatch, output: SkipAt, seconds: float):
    st = _station(tmp_path)
    st.output = output
    output.station = st
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(st, "_scan", lambda path: _done(pcm.TrackScan(1.0, 0, 0, int(seconds * 1000))))
    n = int(seconds * 10)
    monkeypatch.setattr(pcm, "decode", lambda *a: (np.zeros((BLOCK, 2), np.int16) for _ in range(n)))
    return st


def test_skip_ends_a_track_early(tmp_path: Path, monkeypatch) -> None:
    out = SkipAt(after=20)
    st = _playing_station(tmp_path, monkeypatch, out, seconds=120)
    calls = []
    st._play_file(Path("a.mp3"), OnAir("track", "Song"), near_end=lambda end_at, again: calls.append(again))
    assert out.blocks == 20
    assert calls == [False]  # skipped before the prefetch point: gap worded now, once


def test_skip_after_prefetch_words_the_gap_again(tmp_path: Path, monkeypatch) -> None:
    out = SkipAt(after=100)   # 10 s into a 50 s track: past the 45 s prefetch point
    st = _playing_station(tmp_path, monkeypatch, out, seconds=50)
    calls = []
    st._play_file(Path("a.mp3"), OnAir("track", "Song"),
                  near_end=lambda end_at, again: calls.append((end_at, again)))
    assert [again for _, again in calls] == [False, True]
    assert calls[1][0] < calls[0][0] - timedelta(seconds=30)  # re-worded from the real, earlier end


def test_prefetch_again_rewords_the_time_check(tmp_path: Path) -> None:
    st = _station(tmp_path)
    stale = Speech("It's coming up to ten o'clock", "v", _done(None))
    plan = [Step("clock", clock=ClockStep(stale))]
    st._prefetch_gap(plan, datetime(2026, 9, 26, 9, 45), again=True)
    assert plan[0].clock.speech is not stale
    assert "quarter to ten" in plan[0].clock.speech.text


def test_skip_cuts_a_line_short_and_nothing_to_skip_off_air(tmp_path: Path, monkeypatch) -> None:
    out = SkipAt(after=3)
    st = _station(tmp_path)
    st.output, out.station = out, st
    assert not st.skip()          # off air
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    speech = Speech("hello", "v", _done(np.zeros((BLOCK * 20, 2), np.int16)))
    assert st._speak(speech) is False
    assert out.blocks < 20
    assert st._speak(Speech("again", "v", _done(np.zeros((BLOCK, 2), np.int16)))) is True
