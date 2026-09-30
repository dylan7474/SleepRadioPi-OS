"""Programmes: running orders of blocks the radio plays by itself, on a clock."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast import programmes
from sleepradiopi.broadcast.programmes import Scheduler
from sleepradiopi.io import presets
from sleepradiopi.web.server import make_handler

from test_albums import _on_air
from test_ondemand import _od_station

R4 = {"kind": "station", "name": "BBC Radio 4", "url": "http://example.com/radio4"}
RUBBER = {"kind": "album", "root": "music", "folder": "The Beatles/Rubber Soul"}
STORM = {"kind": "album", "root": "ondemand", "folder": "Thunderstorms"}
SUNDAY = datetime(2026, 10, 4, 12, 0)          # a Sunday


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def go(self, **kw): self.t += timedelta(**kw)


def _setup(tmp_path, monkeypatch, progs, start=SUNDAY):
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    clock, woke, slept = Clock(start), [], []
    sched = Scheduler(st, now=clock, wake=lambda: woke.append(1), sleep=lambda: slept.append(1))
    sched.set_programmes(progs)
    st.scheduler = sched
    return st, sched, clock, woke, slept


def test_validate() -> None:
    ok = programmes.validate([{"name": " Sunday  lunch ", "start": "12:00", "then": "sleep", "days": [6, 6],
                               "blocks": [{"name": "Beatles", "items": [RUBBER], "rule": "for", "min": 30},
                                          {"name": "News", "items": [R4], "rule": "at", "at": "13:00", "min": 30}]}])
    assert ok[0]["name"] == "Sunday lunch" and ok[0]["days"] == [6] and ok[0]["auto"] is False
    assert ok[0]["blocks"][1] == {"name": "News", "items": [R4], "rule": "at", "order": "inorder", "min": 30, "at": "13:00"}
    for bad in ([{"name": "A", "blocks": [{"rule": "sometimes"}]}], [{"name": "A"}, {"name": "a"}],
                [{"name": "A", "start": "25:00"}], [{"name": "A", "blocks": [{"rule": "at", "at": "9am"}]}],
                [{"name": "A", "blocks": [{"items": [{"kind": "album", "folder": "../x"}]}]}], [{"name": "A", "then": "explode"}]):
        with pytest.raises(ValueError):
            programmes.validate(bad)


def test_blocks_follow_on_by_their_rules(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "Lunch", "blocks": [
        {"name": "Beatles", "items": [RUBBER], "rule": "for", "min": 30},
        {"name": "Radio 4", "items": [R4], "rule": "until", "until": "13:15"},
        {"name": "Rain", "items": [STORM], "rule": "end"}]}])
    sched.play("lunch")
    assert st.playlist_status()["name"] == "Beatles" and st.source is None      # music: through the show (DJ as set)
    assert sched.status()["until"] == "12:30" and sched.status()["next"] == "Radio 4"
    clock.go(minutes=29); sched.tick()
    assert sched.status()["index"] == 0
    clock.go(minutes=2); sched.tick()
    assert st.source["kind"] == "radio" and sched.status()["until"] == "13:15"
    assert st.requests() == [] and st.playlist_status() is None                 # the Beatles' rest doesn't linger
    clock.go(minutes=45); sched.tick()
    assert st.source["kind"] == "album" and st.source["root"] == "ondemand"     # On demand alone: straight through, no DJ
    assert sched.status()["until"] is None                                      # until it ends
    st._source = None; sched.tick()                                             # (the rain finished)
    assert sched.run is None                                                    # then back to the show


def test_an_at_block_cuts_in_on_time(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Rain", "items": [STORM], "rule": "end"},
        {"name": "The news", "items": [R4], "rule": "at", "at": "13:00", "min": 30}]}], start=SUNDAY.replace(minute=50))
    sched.play("P")
    assert sched.status()["next"] == "The news at 13:00"
    clock.go(minutes=9); sched.tick()
    assert st.source["kind"] == "album"
    clock.go(minutes=1); sched.tick()
    assert st.source["name"] == "BBC Radio 4" and sched.status()["until"] == "13:30"


def test_too_early_for_an_at_block_the_show_fills_in(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Radio 4", "items": [R4], "rule": "for", "min": 10},
        {"name": "Beatles at one", "items": [RUBBER], "rule": "at", "at": "13:00", "min": 30}]}])
    sched.play("P")
    clock.go(minutes=10); sched.tick()
    assert st.source is None and sched.status()["waiting"] is True                # the show until 13:00
    clock.go(minutes=50); sched.tick()
    assert st.playlist_status()["name"] == "Beatles at one"


def test_played_after_an_at_time_it_just_follows_on(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Radio 4", "items": [R4], "rule": "for", "min": 10},
        {"name": "News", "items": [RUBBER], "rule": "at", "at": "13:00", "min": 30}]}], start=SUNDAY.replace(hour=13, minute=10))
    sched.play("P")
    clock.go(minutes=10); sched.tick()
    assert st.playlist_status()["name"] == "News"                                 # not tomorrow at 13:00


def test_choosing_something_else_stops_the_programme(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [{"name": "Radio 4", "items": [R4], "rule": "for", "min": 60}]}])
    sched.play("P")
    st.play_album(root="ondemand", folder="Thunderstorms")                        # picked on the page
    sched.tick()
    assert sched.run is None and st.source["folder"] == "Thunderstorms"


def test_then_sleep_and_repeat(tmp_path, monkeypatch) -> None:
    st, sched, clock, _, slept = _setup(tmp_path, monkeypatch, [
        {"name": "Bed", "then": "sleep", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 20}]},
        {"name": "Loop", "then": "repeat", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 5}]}])
    sched.play("Bed")
    clock.go(minutes=20); sched.tick()
    assert sched.run is None and slept == [1] and st.source is None
    sched.play("Loop")
    clock.go(minutes=5); sched.tick()
    assert sched.run is not None and sched.status()["index"] == 0


def test_starts_by_itself_on_its_days(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _ = _setup(tmp_path, monkeypatch, [
        {"name": "Morning", "start": "07:00", "auto": True, "days": [6], "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 60}]}],
        start=SUNDAY.replace(hour=6, minute=59))
    sched.tick()
    assert sched.run is None
    clock.go(minutes=1); sched.tick()
    assert sched.run["name"] == "Morning" and woke == [1]
    sched.stop(); clock.go(seconds=20); sched.tick()
    assert sched.run is None                                                     # once, not every tick of 07:00
    clock.go(days=1); sched.tick()                                               # Monday: not one of its days
    assert sched.run is None


def test_a_block_that_cant_play_is_skipped(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Gone", "items": [{"kind": "album", "root": "music", "folder": "Nobody/Nothing"}], "rule": "for", "min": 30},
        {"name": "R4", "items": [R4], "rule": "for", "min": 30}]}])
    sched.play("P")
    sched.tick()
    assert st.source["name"] == "BBC Radio 4"


def test_the_show_or_an_artist_list_as_a_block(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Beatles radio", "items": [{"kind": "show", "artist": "The Beatles"}], "rule": "end"}]}])
    st.tune({"kind": "radio", "name": "x", "url": "http://example.com/x"})
    sched.play("P")
    assert st.artist == "The Beatles" and st.source is None
    assert sched.status()["until"] == "13:00"                                    # the show has no end: an hour


def test_on_a_button() -> None:
    p = presets.validate({"kind": "programme", "name": "Sunday lunch"})
    assert presets.label(p) == "Sunday lunch" and presets.same(p, {"kind": "programme", "name": "sunday LUNCH"})


def test_web_api(tmp_path, monkeypatch) -> None:
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        got = req("/api/programmes", {"programmes": [{"name": "Lunch", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 30}]}]})
        assert got["programmes"][0]["name"] == "Lunch" and got["playing"] is None
        assert req("/api/programmes/play", {"name": "Lunch"})["playing"]["block"] == "R4"
        assert req("/api/programmes")["playing"]["name"] == "Lunch"
        assert req("/api/programmes/stop", {})["playing"] is None
        with pytest.raises(urllib.error.HTTPError):
            req("/api/programmes/play", {"name": "Nope"})
    finally:
        httpd.shutdown()


def _sched_with_hooks(tmp_path, monkeypatch, progs, start=SUNDAY):
    st, sched, clock, woke, slept = _setup(tmp_path, monkeypatch, progs, start)
    did = []
    sched.say = lambda text: did.append(("say", text))
    sched.jingle = lambda path: did.append(("jingle", path))
    sched.action = lambda name: did.append(("action", name))
    sched.pause = lambda: did.append(("pause",))
    return st, sched, clock, woke, slept, did


def test_moments_happen_then_the_programme_goes_on(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Radio 4", "items": [R4], "rule": "for", "min": 10},
        {"name": "Dad's note", "items": [{"kind": "message", "text": "Lunch is at half past twelve."}, {"kind": "action", "action": "time"}], "rule": "at", "at": "12:30", "min": 1},
        {"name": "Beatles", "items": [{"kind": "jingle", "path": "ident.mp3"}, RUBBER], "rule": "for", "min": 20}]}])
    sched.play("P")
    clock.go(minutes=30); sched.tick()                  # 12:30: the note, and a time check
    assert did == [("say", "Lunch is at half past twelve."), ("action", "time")]
    sched.tick()                                        # a moment: straight on to the next block
    assert did[-1] == ("jingle", "ident.mp3") and st.playlist_status()["name"] == "Beatles"


def test_gaps_can_be_silent_or_filled(tmp_path, monkeypatch) -> None:
    progs = [{"name": "Quiet", "gap": "silence", "blocks": [
                {"name": "R4", "items": [R4], "rule": "for", "min": 10},
                {"name": "Beatles at one", "items": [RUBBER], "rule": "at", "at": "13:00", "min": 30}]},
             {"name": "Filled", "gap": R4, "blocks": [
                {"name": "Empty hour", "items": [], "rule": "for", "min": 60}]}]
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, progs)
    sched.play("Quiet")
    clock.go(minutes=10); sched.tick()
    assert did == [("pause",)] and sched.status()["waiting"] is True
    clock.go(minutes=50); sched.tick()
    assert woke == [1] and st.playlist_status()["name"] == "Beatles at one"   # sound again for the next block
    sched.play("Filled")                                                      # an empty block: the gap's choice
    assert st.source["name"] == "BBC Radio 4"


def test_when_its_over_stop_keep_or_chain(tmp_path, monkeypatch) -> None:
    progs = [{"name": "A", "then": "chain", "chain": "B", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 5}]},
             {"name": "B", "then": "stop", "blocks": [{"name": "Beatles", "items": [RUBBER], "rule": "for", "min": 5}]},
             {"name": "C", "then": "keep", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 5}]}]
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, progs)
    sched.play("A")
    clock.go(minutes=5); sched.tick()
    assert sched.run["name"] == "B" and st.playlist_status()["name"] == "Beatles"
    clock.go(minutes=5); sched.tick()
    assert sched.run is None and did == [("pause",)]
    sched.play("C")
    clock.go(minutes=5); sched.tick()
    assert sched.run is None and st.source["name"] == "BBC Radio 4"           # left playing


def test_validate_the_new_things() -> None:
    ok = programmes.validate([{"name": "P", "gap": {"kind": "station", "name": "R3", "url": "http://example.com/r3"}, "then": "chain", "chain": "Bedtime",
                               "blocks": [{"items": [{"kind": "message", "text": " Hello "}, {"kind": "action", "action": "news"}, {"kind": "jingle", "path": "a.mp3"}]}]}])
    assert ok[0]["chain"] == "Bedtime" and ok[0]["gap"]["kind"] == "station" and ok[0]["blocks"][0]["items"][0]["text"] == "Hello"
    for bad in ([{"name": "P", "then": "chain"}], [{"name": "P", "gap": {"kind": "message", "text": "x"}}],
                [{"name": "P", "blocks": [{"items": [{"kind": "action", "action": "explode"}]}]}], [{"name": "P", "gap": "loud"}]):
        with pytest.raises(ValueError):
            programmes.validate(bad)


def test_a_one_song_block_isnt_over_before_it_starts(tmp_path, monkeypatch) -> None:
    """A block of one song cuts in as the next thing; until it does it was
    counted as finished, and the programme ended at once (and slept)."""
    st, sched, clock, _, slept = _setup(tmp_path, monkeypatch, [{"name": "Test 1", "then": "sleep", "blocks": [
        {"name": "Donna", "items": [{"kind": "track", "root": "music", "path": "Nick Drake/Pink Moon/01 - Pink Moon.mp3"}], "rule": "end"}]}])
    st._track_started(st._take_next())                  # the show is playing a song
    sched.play("Test 1")
    clock.go(seconds=4); sched.tick()
    assert sched.run is not None and slept == []
    st._track_started(st._jump)                          # it cuts in and plays
    st._jump = None
    sched.tick()
    assert sched.run is not None


def test_armed_once_starts_at_its_time_then_switches_off(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _ = _setup(tmp_path, monkeypatch, [
        {"name": "Once", "start": "12:05", "auto": True, "once": True, "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 5}]}])
    saved = []
    sched.on_change = lambda progs: saved.append(progs)
    assert sched.programmes[0]["once"] is True
    clock.go(minutes=5); sched.tick()
    assert sched.run["name"] == "Once" and woke == [1]
    assert sched.programmes[0]["auto"] is False and "once" not in sched.programmes[0] and saved
    sched.stop(); clock.go(days=1); sched.tick()
    assert sched.run is None                                                     # not again tomorrow
    assert "once" not in programmes.validate([{"name": "A", "auto": False, "once": True}])[0]   # (only with auto)


def test_a_chained_programme_waits_quietly_for_its_first_at_block(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [
        {"name": "Afternoon", "then": "chain", "chain": "Evening", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 30}]},
        {"name": "Evening", "gap": "silence", "blocks": [{"name": "Beatles at six", "items": [RUBBER], "rule": "at", "at": "18:00", "min": 60}]}])
    sched.play("Afternoon")
    clock.go(minutes=30); sched.tick()                       # 12:30: Afternoon's over, Evening takes over...
    assert sched.run["name"] == "Evening" and sched.status()["waiting"] is True and did == [("pause",)]
    assert sched.status()["next"] == "Beatles at six at 18:00"
    clock.go(hours=5, minutes=29); sched.tick()
    assert sched.run["name"] == "Evening" and st.playlist_status() is None      # ...silent until 18:00
    clock.go(minutes=1); sched.tick()
    assert st.playlist_status()["name"] == "Beatles at six" and woke == [1]


def test_programme_mode_is_quiet_unless_a_programme_is_on(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [
        {"name": "Morning", "start": "12:05", "auto": True, "then": "show", "blocks": [
            {"name": "R4", "items": [R4], "rule": "for", "min": 10},
            {"name": "Beatles at half past", "items": [RUBBER], "rule": "at", "at": "12:30", "min": 10}]}])
    sched.set_quiet(True)
    assert did == [("pause",)]                               # on, with nothing playing: quiet at once
    clock.go(minutes=5); sched.tick()
    assert sched.run["name"] == "Morning" and woke == [1]    # an armed programme wakes it (an alarm)
    clock.go(minutes=10); sched.tick()
    assert sched.status()["waiting"] and did[-1] == ("pause",)   # the gap: silent, not the show
    clock.go(minutes=15); sched.tick()
    assert st.playlist_status()["name"] == "Beatles at half past" and woke == [1, 1]
    clock.go(minutes=10); sched.tick()
    assert sched.run is None and did[-1] == ("pause",)       # over: quiet again, though "then" said the show


def test_programme_mode_web(tmp_path, monkeypatch) -> None:
    st, sched, *_ = _setup(tmp_path, monkeypatch, [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        r = urllib.request.Request(base + "/api/programmes/mode", data=json.dumps({"on": True}).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            assert json.load(resp)["programme_mode"] is True and sched.quiet
    finally:
        httpd.shutdown()
