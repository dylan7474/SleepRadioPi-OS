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
    assert st.playlist_status()["name"] == "Beatles" and st.source["kind"] == "playlist"   # music: straight through, no DJ
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
    assert st.profile == "Beatles" and st.source is None                         # (an old artist block: its theme)
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


def test_a_programme_that_doesnt_wake_a_paused_radio(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _ = _setup(tmp_path, monkeypatch, [
        {"name": "News", "start": "12:05", "auto": True, "wake": False, "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 10}]},
        {"name": "Alarm", "start": "12:10", "auto": True, "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 10}]}])
    assert sched.programmes[0]["wake"] is False and "wake" not in sched.programmes[1]
    sched.paused = lambda: True                              # someone pressed pause
    clock.go(minutes=5); sched.tick()
    assert sched.run is None and woke == []                  # the pause wins
    clock.go(minutes=5); sched.tick()
    assert sched.run["name"] == "Alarm" and woke == [1]      # an alarm still wakes it
    sched.stop(); sched.paused = lambda: False
    clock.go(days=1, minutes=-5); sched.tick()               # tomorrow, playing: it starts
    assert sched.run["name"] == "News"


def test_a_talking_clock_every_quarter_of_an_hour_over_whatever_is_on(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _ = _setup(tmp_path, monkeypatch, [
        {"name": "Talking clock", "start": "12:00", "every": 15, "until": "12:30", "auto": True,
         "blocks": [{"name": "The time", "items": [{"kind": "action", "action": "pips"}, {"kind": "action", "action": "time"}]}]},
        {"name": "Radio 4", "blocks": [{"name": "R4", "items": [R4], "rule": "for", "min": 90}]}],
        start=SUNDAY.replace(minute=0) - timedelta(minutes=2))
    signals = []
    sched.time_signal = lambda at, pips, speak: signals.append((at.strftime("%H:%M"), pips, speak))
    sched.play("Radio 4")
    for _ in range(40):                                   # 11:58 to 12:38, a minute at a time
        sched.tick(); clock.go(minutes=1)
    assert signals == [("12:00", True, True), ("12:15", True, True), ("12:30", True, True)]   # got ready a minute early each time
    assert sched.run["name"] == "Radio 4" and st.source["name"] == "BBC Radio 4"               # the programme playing carried on


def test_repeating_every_needs_a_proper_interval() -> None:
    ok = programmes.validate([{"name": "P", "every": 30}])
    assert ok[0]["every"] == 30 and ok[0]["until"] == "23:59"
    with pytest.raises(ValueError):
        programmes.validate([{"name": "P", "every": 7}])


def test_switches_validate() -> None:
    ok = programmes.validate([{"name": "A", "blocks": [{"items": [R4]}], "switches": [{"what": "noise", "from": 30, "min": 90}]}, {"name": "B", "switches": []}])
    assert ok[0]["switches"] == [{"what": "noise", "from": 30, "min": 90}] and "switches" not in ok[1]
    only = programmes.validate([{"name": "N", "start": "19:07", "switches": [{"what": "noise", "from": 173, "min": 540}, {"what": "dj", "from": 180, "min": 5}]}])[0]
    assert only["start"] == "22:00" and only["switches"] == [{"what": "noise", "from": 0, "min": 540}, {"what": "dj", "from": 7, "min": 5}]   # starts with its first switch
    for bad in ({"what": "lights"}, {"what": "dj", "min": 0}, {"what": "dj", "from": -1}, {"what": "noise", "min": True}):
        with pytest.raises(ValueError):
            programmes.validate([{"name": "A", "switches": [bad]}])


def test_switches_on_at_the_start_off_at_the_end_beside_the_blocks(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Radio 4", "items": [R4], "rule": "for", "min": 30},
        {"name": "Beatles", "items": [RUBBER], "rule": "for", "min": 30}],
        "switches": [{"what": "noise", "from": 20, "min": 20}, {"what": "dj", "from": 0, "min": 60}]}])
    sched.play("P")
    assert did == [("action", "dj_on")] and st.source["name"] == "BBC Radio 4"   # (whatever it was before)
    clock.go(minutes=20); sched.tick()
    assert did[-1] == ("action", "noise_on")
    clock.go(minutes=10); sched.tick()                  # the next block: the noise carries on over it
    assert st.playlist_status()["name"] == "Beatles" and did[-1] == ("action", "noise_on")
    clock.go(minutes=10); sched.tick()
    assert did[-1] == ("action", "noise_off")
    clock.go(minutes=20); sched.tick()
    assert did[-1] == ("action", "dj_off") and sched.spans == []


def test_a_noise_only_programme_plays_over_whats_on(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [
        {"name": "Noise", "start": "12:05", "auto": True, "switches": [{"what": "noise", "from": 0, "min": 60}]}])
    st.tune(R4 | {"kind": "radio"})
    clock.go(minutes=5); sched.tick()                   # starts by itself, over the station, no wake
    assert did == [("action", "noise_on")] and woke == [] and sched.run is None and st.source["name"] == "BBC Radio 4"
    assert sched.switches_status()[0]["until"] == "13:05"
    clock.go(minutes=60); sched.tick()
    assert did[-1] == ("action", "noise_off")
    sched.play("Noise")                                 # Play now, then Stop: off at once
    assert sched.run is None and did[-1] == ("action", "noise_on")
    sched.stop(name="noise")
    assert did[-1] == ("action", "noise_off") and sched.spans == []


def test_switches_when_the_programme_gives_way(tmp_path, monkeypatch) -> None:
    st, sched, clock, woke, _, did = _sched_with_hooks(tmp_path, monkeypatch, [{"name": "P", "blocks": [
        {"name": "Radio 4", "items": [R4], "rule": "for", "min": 60}],
        "switches": [{"what": "noise", "from": 0, "min": 30}, {"what": "dj", "from": 40, "min": 10}]}])
    sched.play("P")
    st.play_album(root="music", folder="The Beatles/Rubber Soul")   # something else chosen
    sched.tick()
    assert sched.run is None and [s["what"] for s in sched.spans] == ["noise"]   # started: runs on; the DJ's: dropped
    clock.go(minutes=30); sched.tick()
    assert did == [("action", "noise_on"), ("action", "noise_off")] and sched.spans == []


def test_a_programme_that_starts_when_its_started(tmp_path, monkeypatch) -> None:
    """No time of day: its clock is at 00:00 when it's started, whenever that is. It never starts by itself."""
    ok = programmes.validate([{"name": "Bedtime", "anytime": True, "start": "21:30", "auto": True, "days": [1], "every": 30,
                               "blocks": [{"name": "R4", "items": [R4], "rule": "until", "until": "00:20"}]}])[0]
    assert ok["anytime"] is True and ok["start"] == "00:00" and ok["auto"] is False and ok["days"] == [] and "every" not in ok
    said = []
    st, sched, clock, *_ = _setup(tmp_path, monkeypatch, [{"name": "Bedtime", "anytime": True, "then": "stop", "blocks": [
        {"name": "Radio", "items": [R4], "rule": "until", "until": "00:20"},         # to twenty minutes in
        {"name": "Rain", "items": [STORM], "rule": "at", "at": "00:30", "min": 15},  # from half an hour in
        {"name": "Goodnight", "items": [{"kind": "message", "text": "Goodnight."}], "rule": "at", "at": "00:40", "min": 1}],
        "switches": [{"what": "noise", "from": 5, "min": 10}]}], start=SUNDAY.replace(hour=22, minute=47))
    sched.say = said.append
    for _ in range(3):
        clock.go(hours=1); sched.tick()
    assert sched.run is None                                 # (armed or not, it doesn't start by itself)
    sched.play("Bedtime")                                    # 01:47
    s = sched.status()
    assert s["anytime"] is True and s["started"] == "01:47" and s["until"] == "02:07" and st.source["name"] == "BBC Radio 4"
    assert [(w["on"].strftime("%H:%M"), w["off"].strftime("%H:%M")) for w in sched.spans] == [("01:52", "02:02")]
    clock.go(minutes=19); sched.tick()
    assert st.source["name"] == "BBC Radio 4"
    clock.go(minutes=1); sched.tick()                        # 20 minutes in: the station's time is up; too early for the rain
    assert sched.status()["waiting"] is True
    clock.go(minutes=10); sched.tick()                       # 30 minutes in
    assert st.source["folder"] == "Thunderstorms" and sched.status()["until"] == "02:32"

    # only moments: each at its time after the start, not all at once
    st, sched, clock, *_ = _setup(tmp_path / "b", monkeypatch, [{"name": "Chimes", "anytime": True, "blocks": [
        {"name": "Now", "items": [{"kind": "message", "text": "Starting."}], "rule": "at", "at": "00:00", "min": 1}]}])
    sched.say = said.append
    sched.play("Chimes")
    assert said == ["Starting."] and sched.run is None
