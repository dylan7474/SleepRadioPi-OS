"""The knob pressed and turned: back or on through a book, podcast or song."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.io.knob import Knob
from sleepradiopi.io.seek import KnobSeek

from test_albums import _on_air
from test_audiobooks import Out, _station as _book_station, books_dir, fake_decode   # noqa: F401 (fixtures)
from test_ondemand import _od_station

MICHELLE = ["music", "The Beatles/Rubber Soul/02 - Michelle.mp3"]


def test_a_turn_while_pressed_seeks_and_the_press_does_nothing(tmp_path: Path) -> None:
    turns, held, presses, longs = [], [], [], []
    k = Knob(turns.append, lambda: presses.append(1), devices=tmp_path, on_long_press=lambda: longs.append(1),
             on_held_turn=held.append)
    k.on_turn(1)
    k.on_press(); k.on_turn(-1); k.on_turn(-1); k.on_release()     # pressed and turned
    k.on_turn(2)
    k.on_press(); k.on_release()                                   # an ordinary press
    assert turns == [1, 2] and held == [-1, -1] and presses == [1] and longs == []


def test_a_book_moves_half_a_minute_a_click(tmp_path, books_dir) -> None:
    st = _book_station(tmp_path, books_dir)
    assert st.knob_seek(1) is None                                 # the show: nothing to move
    st.tune({"kind": "book", "key": "The Hobbit.m4b"})
    where = st.knob_seek(1)                                        # (a 5 s book: to its last second)
    assert where == {"path": books_dir / "The Hobbit.m4b", "ms": 4000}
    assert st.book_positions.get("The Hobbit.m4b") == 4000
    assert st.knob_seek(-1)["ms"] == 0


def test_a_paused_song_moves_and_plays_on_from_there(tmp_path, fake_decode) -> None:
    st = _od_station(tmp_path)
    st.add_to_playlist("Mix", [MICHELLE])
    st.play_playlist("Mix")
    assert st.knob_seek(2)["ms"] == 20_000 and st.source["offset_ms"] == 20_000
    assert st.knob_seek(-5)["ms"] == 0
    st.knob_seek(1)
    st._switch.clear()
    st._run_album(st.source)
    assert fake_decode[0] == ("02 - Michelle.mp3", 10_000, 0)


def test_a_playing_song_jumps(tmp_path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    calls = []

    def decode(path, start_ms=0, end_ms=0):
        calls.append(start_ms)
        return (np.zeros((2205, 2), np.int16) for _ in range(5 if len(calls) == 1 else 2))
    monkeypatch.setattr(pcm, "decode", decode)
    written = []

    def write(block):
        if len(block) != 2205:
            return                                                  # (silence while its scan was awaited)
        written.append(1)
        if len(written) == 2:
            assert st.knob_seek(3)["ms"] == 30_050                   # 50 ms in, then three clicks on
    monkeypatch.setattr(st, "_write", write)
    st.add_to_playlist("Mix", [MICHELLE])
    st.play_playlist("Mix")
    st._switch.clear()
    st._run_album(st.source)
    assert calls == [0, 30_050] and len(written) == 4


def test_paused_inside_a_song_it_waits_and_carries_on_from_where_it_was_moved(tmp_path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    calls = []

    def decode(path, start_ms=0, end_ms=0):
        calls.append(start_ms)
        return (np.zeros((2205, 2), np.int16) for _ in range(5 if len(calls) == 1 else 1))
    monkeypatch.setattr(pcm, "decode", decode)
    paused = [False]
    st.speaker_paused = lambda: paused[0]

    def write(block):
        if len(block) == 2205 and len(calls) == 1 and not paused[0]:
            paused[0], st._listeners = True, 0                      # the knob pressed: paused

            def later():
                time.sleep(0.3)
                st.knob_seek(1)                                     # moved while paused
                paused[0], st._listeners = False, 1                 # and played again
            threading.Thread(target=later, daemon=True).start()
    monkeypatch.setattr(st, "_write", write)
    st._listeners = 1
    st.add_to_playlist("Mix", [MICHELLE])
    st.play_playlist("Mix")
    st._switch.clear()
    st._run_album(st.source)
    assert calls == [0, 10_050]                                     # (50 ms in when paused)


def test_paused_a_snatch_plays_every_half_second_while_it_moves(monkeypatch) -> None:
    from sleepradiopi.io import seek as seek_mod
    monkeypatch.setattr(seek_mod, "SNATCH_EVERY_S", 0.05)
    monkeypatch.setattr(seek_mod, "IDLE_S", 0.2)

    class St:
        ms = 0
        def knob_seek(self, clicks):
            self.ms += clicks * 1000
            return {"path": Path("a.mp3"), "ms": self.ms}
        def snatch(self, path, ms, length):
            return np.full((10, 2), ms, np.int16)

    class Ctl:
        paused = True
        clips = []
        def play_clip(self, clip):
            self.clips.append(int(clip[0][0][0]))

    ctl = Ctl()
    seek = KnobSeek(St(), ctl, lambda audio, kind, label: (audio, kind, label))
    seek.turn(1, speed_up=False)
    seek.turn(1, speed_up=False)
    time.sleep(0.6)
    assert ctl.clips and ctl.clips[-1] == 2000                     # where it got to
    assert seek._thread is None                                    # stopped once it stopped moving
    ctl.paused, ctl.clips[:] = False, []
    seek.turn(1, speed_up=False)                                   # playing: it just jumps
    time.sleep(0.2)
    assert ctl.clips == []
