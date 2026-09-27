from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.broadcast.models import BroadcastTrack, LinkKind
from sleepradiopi.broadcast.script_builder import DjScriptBuilder
from sleepradiopi.config.clock import ENV, clock_trusted
from sleepradiopi.config.settings import Settings


class FakeTts:
    ready = True

    def synth(self, voice, text, speed):
        return np.zeros(100, dtype=np.float32), 22050


class NullOutput:
    def start(self): ...
    def write(self, block): ...
    def stop(self): ...


def _station(tmp_path: Path) -> station_mod.Station:
    music = tmp_path / "music" / "Artist" / "Album"
    music.mkdir(parents=True)
    for n in (1, 2, 3):
        (music / f"0{n} - Song {n}.mp3").write_bytes(b"x")
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "none",
               hooks_file="", scan_cache=tmp_path / "scans.json", tag_cache=None)
    return station_mod.Station(cfg, FakeTts(), NullOutput())


def test_clock_trusted_follows_the_flag_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert clock_trusted()                       # desktop: no flag configured
    flag = tmp_path / "time-synced"
    monkeypatch.setenv(ENV, str(flag))
    assert not clock_trusted()                   # appliance, not synced yet
    flag.touch()
    assert clock_trusted()


def test_no_time_of_day_greeting_without_a_clock() -> None:
    assert DjScriptBuilder().welcome_greeting(time_known=False) == "Hello, and welcome to Sleep Radio."


@pytest.mark.parametrize("synced", [False, True])
def test_time_checks_and_news_only_with_a_trusted_clock(tmp_path: Path, monkeypatch, synced) -> None:
    flag = tmp_path / "time-synced"
    if synced:
        flag.touch()
    monkeypatch.setenv(ENV, str(flag))
    st = _station(tmp_path)
    monkeypatch.setattr(st._show_clock, "on_track_started", lambda now: LinkKind.TIME_CHECK)
    prev = BroadcastTrack(Path("a.mp3"), "Song 1", "Artist")
    nxt = BroadcastTrack(Path("b.mp3"), "Song 2", "Artist")
    kinds = [s.kind for s in st._plan_gap(prev, nxt)]
    assert ("clock" in kinds) == synced

    fetched = []
    monkeypatch.setattr(st.news_repo, "headlines_for", lambda slot: fetched.append(slot) or [])
    slot = type("Slot", (), {"value": "top"})()
    monkeypatch.setattr(st.news_schedule, "prep_at", lambda now: type("Due", (), {"key": 1, "slot": slot})())
    st._maybe_prepare_news()
    for t in list(station_mod.threading.enumerate()):
        if t.name == "news-prep":
            t.join(2)
    assert bool(fetched) == synced
