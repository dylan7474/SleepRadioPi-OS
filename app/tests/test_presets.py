import json
import struct
import threading
import urllib.error
import urllib.request
from concurrent.futures import Future
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.broadcast import programmes
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load
from sleepradiopi.io import knob, presets as presets_mod
from sleepradiopi.io.presets import Presets
from sleepradiopi.web.server import make_handler

from test_offline import _station

RP = {"kind": "radio", "name": "Radio Paradise", "url": "http://rp/", "info": ""}


def _done(value) -> Future:
    f = Future()
    f.set_result(value)
    return f


class Out:
    """Counts blocks; calls then(n) after each."""

    def __init__(self, then=None):
        self.blocks, self.then = 0, then

    def start(self): ...
    def stop(self): ...

    def write(self, block):
        self.blocks += 1
        if self.then:
            self.then(self.blocks)


def _album_station(tmp_path, monkeypatch, blocks_per_track=5):
    st = _station(tmp_path)
    st.tts, st._opening = None, None
    monkeypatch.setattr(st, "_scan", lambda path: _done(pcm.TrackScan(1.0, 0, 0, 10_000)))
    monkeypatch.setattr(pcm, "decode",
                        lambda *a, **k: (np.zeros((4410, 2), np.int16) for _ in range(blocks_per_track)))
    return st


# --- albums straight through -------------------------------------------------------------

def test_an_album_plays_straight_through_then_the_show(tmp_path: Path, monkeypatch) -> None:
    st = _album_station(tmp_path, monkeypatch)
    saved = []
    st.on_source = saved.append
    album = st.albums()[0]
    assert album["folder"] == "Artist/Album"
    st.play_album(album["id"])
    assert saved[-1] == {"kind": "album", "folder": "Artist/Album", "title": album["title"],
                         "artist": album["artist"], "track": 0}
    titles = []
    st.output = Out()
    st._switch.clear()
    real_play = st._play_file

    def play_file(path, on_air, near_end=None, **kw):
        titles.append(on_air.title)
        real_play(path, on_air, near_end, **kw)
    monkeypatch.setattr(st, "_play_file", play_file)
    st._run_album(st.source)
    assert titles == [t.title for t in album["tracks"]]      # in order, all of them
    assert st.source is None and saved[-1] is None             # then back to the show (saved)
    assert [s["track"] for s in saved[1:-1]] == [0, 1, 2]      # each track saved, to resume at


def test_an_album_resumes_at_its_track_and_skip_goes_to_the_next(tmp_path: Path, monkeypatch) -> None:
    st = _album_station(tmp_path, monkeypatch, blocks_per_track=50)
    album = st.albums()[0]
    st.tune({"kind": "album", "folder": album["folder"], "track": 1})
    heard = []

    def then(n):
        heard.append(st.on_air.title)
        if n == 3:
            assert st.skip()                                    # skip works on an album
    st.output = Out(then)
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st._switch.clear()
    st._run_album(st.source)
    assert heard[0] == album["tracks"][1].title                 # resumed at track 2
    assert heard.count(album["tracks"][1].title) == 3 and heard[-1] == album["tracks"][2].title
    assert st.status()["source"] is None


def test_album_sources_are_checked(tmp_path: Path) -> None:
    st = _station(tmp_path)
    with pytest.raises(ValueError, match="library"):
        st.tune({"kind": "album", "folder": "Nobody/Nothing"})
    with pytest.raises(ValueError):
        st.play_album(99)
    with pytest.raises(ValueError):
        st.tune({"kind": "cassette"})
    st.tune({"kind": "album", "folder": "Artist/Album", "track": 99})
    assert st.source["track"] == 2                              # clamped to the album


# --- presets -----------------------------------------------------------------------------

def test_validation_and_labels() -> None:
    assert presets_mod.validate(None) is None
    assert presets_mod.validate({"kind": "radio", "name": "RP", "url": "http://rp/"}) == RP | {"name": "RP"}
    assert presets_mod.validate({"kind": "show", "artist": "The Beatles", "profile": "Friday"}) == \
        {"kind": "show", "artist": None, "profile": "Friday"}
    assert presets_mod.validate({"kind": "action", "action": "time"}) == {"kind": "action", "action": "time"}
    for bad in ({"kind": "action", "action": "explode"}, {"kind": "album"}, {"kind": "radio", "name": "x"},
                {"kind": "tape"}, "RP", {"kind": "show", "artist": 5}):
        with pytest.raises(ValueError):
            presets_mod.validate(bad)
    assert presets_mod.validate_all([None]) == [None] * 4
    assert len(presets_mod.validate_all([None], 6)) == 6          # the cathedral's six
    with pytest.raises(ValueError):
        presets_mod.validate_all([None] * 7)
    assert presets_mod.label(RP) == "Radio Paradise"
    assert presets_mod.label({"kind": "album", "folder": "a", "title": "Rubber Soul", "artist": "The Beatles"}) \
        == "Rubber Soul — The Beatles"
    old = presets_mod.validate({"kind": "action", "action": "sleep"})      # (saved before the sleep times: 30 minutes)
    assert old == {"kind": "action", "action": "sleep_30"} and presets_mod.label(old) == "Sleep in 30 minutes"
    assert presets_mod.label(presets_mod.validate({"kind": "action", "action": "sleep_5"})) == "Sleep in 5 minutes"
    with pytest.raises(ValueError):
        presets_mod.validate({"kind": "action", "action": "sleep_7"})
    assert presets_mod.label(None) == "Empty"


class Control:
    def __init__(self):
        self.calls, self.clips, self.sleep = [], [], 0

    def toggle(self): self.calls.append("toggle")
    def play(self): self.calls.append("play")
    def play_clip(self, clip): self.clips.append(clip.label)
    def set_sleep(self, m): self.sleep = m
    def set_noise(self, on=None, kind=None):
        self.calls.append(f"noise {'on' if on else 'off'}" + (f" {kind}" if kind else ""))
        self.noise = {"on": bool(on), "kind": kind or "pink"}
    def status(self): return {"sleep_min": self.sleep or None, "noise": getattr(self, "noise", {"on": False, "kind": "pink"})}


def _presets(tmp_path, buttons=None):
    st = _station(tmp_path)
    st.tts = None                                    # beeps only: no speech threads
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    ctl = Control()
    return st, ctl, conf, Presets(st, ctl, conf, buttons)


def test_messages_jingles_and_birthdays_on_buttons(tmp_path: Path) -> None:
    """Every object can go on a button: these happen over what's on, nothing paused."""
    msg, jin, bd = {"kind": "message", "text": " Tea  time "}, {"kind": "jingle", "path": "/Station ID.mp3"}, {"kind": "birthday", "name": "Mum", "day": 3, "month": 5}
    assert presets_mod.validate(msg) == {"kind": "message", "text": "Tea time"}
    assert presets_mod.validate(jin) == {"kind": "jingle", "path": "Station ID.mp3"}
    assert presets_mod.validate(bd)["name"] == "Mum"
    for bad in ({"kind": "message", "text": " "}, {"kind": "jingle", "path": "../x.mp3"}, {"kind": "birthday", "name": "Mum"}):
        with pytest.raises(ValueError):
            presets_mod.validate(bad)
    st, ctl, conf, p = _presets(tmp_path, [msg, jin, bd])
    assert [p.label(x) for x in p.presets[:3]] == ["“Tea time”", "Station ID", "Mum's birthday"]
    played, said = [], []
    p.jingle = played.append
    p._say = lambda text, beep_first=True, always=False: said.append((text, always))
    p.press(0); p.press(1); p.press(2)
    assert played == ["Station ID.mp3"] and said[0][1] and "Tea time" in said[0][0] and "Mum" in said[1][0]
    assert ctl.calls == []                                                    # (nothing played or paused)
    p.set(3, presets_mod.validate({"kind": "action", "action": "pips"}))       # the pips, too
    assert p.label(p.presets[3]) == "The pips"
    p.press(3)
    assert ctl.clips[-1] == "The pips" and ctl.calls == []


def test_a_programme_sets_things_on_or_off_where_a_button_switches(tmp_path: Path) -> None:
    """A programme's moment says on or off: a switch at a set time could go either way."""
    st, ctl, conf, p = _presets(tmp_path)
    p.scheduled("noise_on"); p.scheduled("noise_off")
    assert ctl.calls == ["noise on", "noise off"]
    st.set_dj(dj_on=True)
    p.scheduled("dj_on")
    assert st.dj_on
    p.scheduled("dj_off"); p.scheduled("dj_off")
    assert not st.dj_on and json.loads(conf.read_text())["broadcast_dj"] is False
    p.scheduled("sleep_15")
    assert ctl.sleep == 15
    ctl.sleep = 12; p.scheduled("sleep_60"); p.scheduled("sleep")          # (already counting down: left be)
    assert ctl.sleep == 12
    old = programmes.validate([{"name": "P", "blocks": [{"name": "B", "items": [{"kind": "action", "action": "sleep"}]}]}])
    assert old[0]["blocks"][0]["items"] == [{"kind": "action", "action": "sleep_30"}]      # (saved before the sleep times)
    for a in ("address", "noise_on", "noise_off", "dj_on", "dj_off", "sleep_5", "sleep_90"):
        assert programmes.validate([{"name": "P", "blocks": [{"name": "B", "items": [{"kind": "action", "action": a}]}]}])


def test_a_press_plays_the_preset_and_never_pauses(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [RP])
    p.press(0)
    assert st.source["url"] == "http://rp/" and ctl.calls == ["play"]
    p.press(0)                                       # already playing: it plays (pausing is the knob's)
    assert ctl.calls == ["play", "play"] and ctl.clips == []      # (one thing on it: no pips)
    assert p.status()["buttons"][0]["playing"]


RP2 = {"kind": "radio", "name": "Radio Two", "url": "http://r2/", "info": ""}
RP3 = {"kind": "radio", "name": "Radio Three", "url": "http://r3/", "info": ""}


def _settle(p, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while p._pending is not None and time.monotonic() < end:
        time.sleep(0.01)
    time.sleep(0.03)


def test_a_button_with_steps_moves_on_one_each_press(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(presets_mod, "STEP_SETTLE_S", 0.05)
    st, ctl, conf, p = _presets(tmp_path, [[RP, RP2, RP3], RP])
    assert load(conf).buttons == [] and p.presets[0] == [RP, RP2, RP3]
    p.press(0)
    assert st.source is None                         # (not yet: it waits to see if it's pressed again)
    _settle(p)
    assert st.source["url"] == "http://rp/" and ctl.calls == ["play"] and ctl.clips == ["Button"]   # one pip
    p.press(0); _settle(p)                           # again: the next one
    assert st.source["url"] == "http://r2/"
    b = p.status()["buttons"][0]
    assert b["label"] == "Radio Two" and b["step"] == 1 and [x["playing"] for x in b["steps"]] == [False, True, False]
    p.press(0); p.press(0); _settle(p)               # twice quickly: past Radio Three, round to the first
    assert st.source["url"] == "http://rp/" and ctl.calls == ["play"] * 3      # (Radio Three never tuned)
    p.press(0); _settle(p)
    assert st.source["url"] == "http://r2/"
    st.tune(RP3 | {"url": "http://else/"})           # something else is put on...
    p.press(0); _settle(p)
    assert st.source["url"] == "http://r2/"           # ...the button comes back to where it was
    p.press(0, 2)                                    # the page's own key for a step: at once
    assert st.source["url"] == "http://r3/"
    with pytest.raises(ValueError):
        p.press(0, 3)


def test_steps_are_checked_saved_and_follow_renames(tmp_path: Path) -> None:
    pl = {"kind": "playlist", "name": "Sunday", "shuffle": False}
    assert presets_mod.validate_button([RP]) == RP and presets_mod.validate_button([]) is None
    assert presets_mod.validate_button([RP, None, pl]) == [RP, pl]
    with pytest.raises(ValueError, match="6 steps"):
        presets_mod.validate_button([RP] * 7)
    with pytest.raises(ValueError):
        presets_mod.validate_button([RP, {"kind": "tape"}])
    assert len(presets_mod.step_pips(3)) > len(presets_mod.step_pips(1))
    st, ctl, conf, p = _presets(tmp_path)
    p.set(0, [RP, pl])
    assert load(conf).buttons[0] == [RP, pl]
    p.add(0, RP2)
    p.add(1, RP2, bank="night")
    assert p.presets[0] == [RP, pl, RP2] and p.banks["night"][1] == RP2
    assert p.rename_playlist("sunday", "Monday") and load(conf).buttons[0][1]["name"] == "Monday"
    assert backup.parse(backup.export(conf, None))[0]["buttons"][0][2] == RP2
    p.set(0, [RP])
    assert p.presets[0] == RP                        # one thing: saved as it always was
    with pytest.raises(ValueError, match="6 steps"):
        p.set(0, [RP] * 7)


def test_a_step_says_its_name_once_its_been_made(tmp_path: Path, monkeypatch) -> None:
    """Names are made in the background when the buttons change; a press only plays a file."""
    from sleepradiopi.io.button_names import Names
    monkeypatch.setattr(presets_mod, "STEP_SETTLE_S", 0.05)
    st, ctl, conf, p = _presets(tmp_path, [[RP, RP2], RP3])
    made, voice, ready = [], ["amy"], [False]

    def render(text):
        made.append(text)
        return np.full((500, 2), 7, np.int16)
    st.tts, st.dj_voice = object(), "amy"                  # (a voice: the DJ's on)
    played = []
    ctl.play_clip = lambda clip: played.append(len(clip.audio))
    names = p.names = Names(render, tmp_path / "names", key=lambda: voice[0], ready=lambda: ready[0], between_s=0)
    monkeypatch.setattr("sleepradiopi.io.button_names.WAIT_READY_S", 0.02)
    p.bake()
    p.press(0); _settle(p)                           # not made yet (the show isn't under way): pips
    assert made == [] and played == [len(presets_mod.step_pips(1))]
    assert p.status()["buttons"][0]["steps"][0]["named"] is False and "named" not in p.status()["buttons"][1]["steps"][0]
    ready[0] = True
    names._queue.join()
    assert sorted(made) == ["Radio Paradise", "Radio Two"]        # (not Radio Three: one thing on its button)
    assert p.status()["buttons"][0]["steps"][1]["named"] is True
    p.press(0); _settle(p)                           # step two: its name, from the file
    assert played[-1] == 500 and len(made) == 2
    st.set_dj(dj_on=False)
    p.press(0); _settle(p)                           # the DJ off: pips
    assert played[-1] == len(presets_mod.step_pips(1))
    st.set_dj(dj_on=True)
    p.set(0, [RP, RP3])                              # Radio Two off the button: its file goes, Radio Three's is made
    names._queue.join()
    assert made[-1] == "Radio Three" and len(list((tmp_path / "names").glob("*.raw"))) == 2
    voice[0] = "joe"                                 # another voice: pips this once, and the name's made again
    p.press(0, 1)
    assert played[-1] == len(presets_mod.step_pips(2))
    names._queue.join()
    assert made[-1] == "Radio Three" and len(made) == 4
    assert presets_mod.spoken({"kind": "playlist", "name": "Sunday", "shuffle": True}, "Sunday (shuffled)") == "Sunday"
    assert presets_mod.spoken({"kind": "album"}, "Rubber Soul — The Beatles") == "Rubber Soul, The Beatles"


def test_actions_in_the_steps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(presets_mod, "STEP_SETTLE_S", 0.05)
    st, ctl, conf, p = _presets(tmp_path, [[RP, {"kind": "action", "action": "sleep"}]])
    p.press(0); _settle(p)
    p.press(0); _settle(p)                           # the sleep timer: its own pip, the station stays on
    assert ctl.sleep == presets_mod.SLEEP_MIN and st.source["url"] == "http://rp/"
    p.press(0); _settle(p)                           # straight after: on round to the station (already on)
    assert ctl.sleep == presets_mod.SLEEP_MIN and ctl.calls == ["play", "play"]
    monkeypatch.setattr(presets_mod, "STEP_RESET_S", 0.0)
    p.press(0); _settle(p)                           # later: the one after what's playing
    assert ctl.sleep == 0


def test_sleep_times_on_buttons(tmp_path: Path) -> None:
    """Each sleep time is its own thing: pressed again it's off, another time's button switches to that time."""
    st, ctl, conf, p = _presets(tmp_path, [{"kind": "action", "action": "sleep_15"}, {"kind": "action", "action": "sleep_60"}])
    said = []
    p._say = lambda text, **kw: said.append(text)    # (never words: just a pip)
    p.press(0)
    assert ctl.sleep == 15
    p.press(1)                                       # a different time: over to it, not off
    assert ctl.sleep == 60
    p.press(1)
    assert ctl.sleep == 0
    ctl.sleep = 30                                   # (set from the page: a 15 button still starts its 15)
    p.press(0)
    assert ctl.sleep == 15 and ctl.clips == ["Button"] * 4 and said == []
    assert p.instant({"kind": "action", "action": "sleep_15"}) and ctl.sleep == 0      # the desktop's double-click: the same
    ctl.slept = True                                 # the timer ran out and paused the radio:
    p.press(0)                                       # the press plays it again, for another 15 minutes
    assert ctl.calls == ["play"] and ctl.sleep == 15 and ctl.clips == ["Button"] * 6


def test_show_and_album_presets(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [
        {"kind": "show", "artist": "Artist"},
        {"kind": "album", "folder": "Artist/Album", "title": "Album", "artist": "Artist"},
        {"kind": "album", "folder": "Gone/Away", "title": "Gone", "artist": "Away"}])
    st.tune(RP)
    p.press(0)                                       # (an old artist button: its one-artist theme)
    assert st.source is None and st.profile == "Artist"
    assert load(conf).broadcast_profile == "Artist" and load(conf).broadcast_artist is None
    p.press(1)
    assert st.source["kind"] == "album" and st.source["folder"] == "Artist/Album"
    before = st.source
    p.press(2)                                       # not in this library: says sorry, keeps playing
    assert st.source is before and ctl.clips == ["Button"]


def test_a_hold_is_just_a_press(tmp_path: Path) -> None:
    """A button is set from the page: one held too long can't wipe what's on it."""
    st, ctl, conf, p = _presets(tmp_path, [RP])
    st.tune(RP2)
    p.hold(0)
    assert p.presets[0] == RP and st.source["url"] == "http://rp/" and ctl.calls == ["play"]


def test_empty_buttons_and_actions(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [None, {"kind": "action", "action": "sleep"},
                                           {"kind": "action", "action": "time"}])
    p.press(0)
    assert ctl.calls == [] and ctl.clips == ["Button"]
    said = []
    p._say = lambda text, **kw: said.append(text)    # (the sleep timer never talks: just a pip)
    p.press(1)
    assert ctl.sleep == presets_mod.SLEEP_MIN
    p.press(1)
    assert ctl.sleep == 0
    assert ctl.clips == ["Button"] * 3 and said == []
    del p._say
    p.press(2)                                       # (no voice here: the beep)
    assert ctl.calls == []


def test_the_buttons_keys_reach_their_button_and_the_knob_keeps_its_own() -> None:
    got = []
    keys = {2: (lambda: got.append("1 down"), lambda: got.append("1 up")),
            5: (lambda: got.append("4 down"), lambda: got.append("4 up"))}

    def ev(code, value):
        return knob.EVENT.pack(0, 0, knob.EV_KEY, code, value)
    data = ev(2, 1) + ev(2, 0) + ev(164, 1) + ev(5, 1) + ev(5, 2) + ev(5, 0)
    knob.handle(data, lambda n: None, lambda: got.append("knob"), None, keys)
    assert got == ["1 down", "1 up", "knob", "4 down", "4 up"]


def test_web_api_and_backups(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf, presets=p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, d = call("/api/buttons")
        assert code == 200 and [b["label"] for b in d["buttons"]] == ["Empty"] * 4
        assert d["now"]["label"] == "Sleep Radio" and "time" in d["actions"]
        code, d = call("/api/buttons", {"button": 2, "preset": RP})
        assert d["buttons"][1]["label"] == "Radio Paradise"
        code, d = call("/api/buttons/press", {"button": 2})
        assert st.source["url"] == "http://rp/" and d["buttons"][1]["playing"]
        code, d = call("/api/buttons", {"button": 3, "now": True})
        assert d["buttons"][2]["preset"] == RP
        code, d = call("/api/buttons", {"button": 3, "preset": RP2, "add": True})
        assert [x["label"] for x in d["buttons"][2]["steps"]] == ["Radio Paradise", "Radio Two"] and d["max_steps"] == 6
        code, d = call("/api/buttons", {"button": 4, "steps": [RP2, RP], "bank": "night"})
        assert [x["label"] for x in d["sets"]["night"][3]["steps"]] == ["Radio Two", "Radio Paradise"]
        code, d = call("/api/buttons/press", {"button": 3, "step": 2})
        assert code == 200 and st.source["url"] == "http://r2/" and d["buttons"][2]["step"] == 1
        assert call("/api/buttons/press", {"button": 3, "step": 3})[0] == 400
        assert call("/api/buttons", {"button": 3, "steps": [RP] * 7})[0] == 400
        code, d = call("/api/buttons", {"button": 5, "preset": None})
        assert code == 400
        code, d = call("/api/buttons", {"button": 1, "preset": {"kind": "action", "action": "nope"}})
        assert code == 400
        code, d = call("/api/album/play", {"id": 0})
        assert code == 200 and d["source"]["kind"] == "album"
        assert call("/api/status")[1]["buttons_bank"] == "day"            # the desktop's "Follow the radio" look
        p.set_bank("night")
        assert call("/api/status")[1]["buttons_bank"] == "night"
    finally:
        httpd.shutdown()
    saved = backup.export(conf, None)
    assert saved["settings"]["buttons"][1] == RP
    saved["settings"]["buttons"] = [{"kind": "action", "action": "nope"}]
    with pytest.raises(backup.BadSettings, match="buttons"):
        backup.parse(saved)


def test_a_list_chosen_while_something_else_plays_starts_with_its_own_songs(tmp_path: Path) -> None:
    """The Dad list, pressed while an audiobook played, opened with a song that
    wasn't on it: the song the old choice had lined up."""
    from dataclasses import asdict
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.config.settings import Settings
    from test_offline import FakeTts, NullOutput
    for artist in ("ABBA", "Frank Sinatra", "The Beatles"):
        d = tmp_path / "music" / artist / "Hits"
        d.mkdir(parents=True)
        for n in range(1, 6):
            (d / f"0{n} - {artist} song {n}.mp3").write_bytes(b"x")
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "none", hooks_file="",
               scan_cache=tmp_path / "scans.json", tag_cache=None,
               profiles=[{"name": "Dad", "artists": ["Frank Sinatra"]}])
    for _ in range(20):                                     # (the lined-up song is random: try often)
        st = station_mod.Station(cfg, FakeTts(), NullOutput())
        st.tts = None
        st._refill()
        st._in_music = False                                # a station / album / book is on instead
        st._source = RP
        p = Presets(st, None, None, [{"kind": "show", "profile": "Dad"}])
        p.press(0)
        assert st.source is None and st.profile == "Dad"
        steps, first = st._take_opening()
        assert first.artist == "Frank Sinatra"
        assert all(t.artist == "Frank Sinatra" for t in list(st._queue)[:3])


def test_day_and_night_sets(tmp_path) -> None:
    import json
    from sleepradiopi.io import presets as pm
    conf = tmp_path / "config.json"
    conf.write_text("{}")

    class Station:
        artist = profile = source = None
        _has_voice = False
    p = pm.Presets(Station(), None, conf, [{"kind": "action", "action": "time"}], None,
                   [{"kind": "action", "action": "sleep"}], "day")
    assert p.presets[0]["action"] == "time" and p.status()["bank"] == "day"
    p.toggle_bank()
    assert p.bank == "night" and p.presets[0]["action"] == "sleep_30"
    p.set(1, {"kind": "action", "action": "news"})               # changes the night set only
    saved = json.loads(conf.read_text())
    assert saved["buttons_bank"] == "night" and saved["buttons_night"][1]["action"] == "news"
    assert saved["buttons"][1] is None
    p.set_bank("day", announce=False)
    assert p.presets[1] is None


def test_the_timetable_swaps_at_its_times_and_a_swap_by_hand_lasts_till_the_next() -> None:
    from sleepradiopi.io import presets as pm

    class Station:
        artist = profile = source = None
        _has_voice = False
    auto = {"on": True, "night_min": 21 * 60, "day_min": 7 * 60}
    assert pm.scheduled_bank(auto, 22 * 60) == "night" and pm.scheduled_bank(auto, 3 * 60) == "night"
    assert pm.scheduled_bank(auto, 12 * 60) == "day"
    assert pm.scheduled_bank({**auto, "night_min": 60, "day_min": 7 * 60}, 30) == "day"   # (night after midnight)
    assert pm.scheduled_bank({**auto, "on": False}, 22 * 60) is None
    p = pm.Presets(Station(), None, None, [], None, [], "day", auto)
    p.check_auto(12 * 60)
    assert p.bank == "day"
    p.check_auto(21 * 60 + 1)                                     # 9 pm: night
    assert p.bank == "night"
    p.set_bank("day", announce=False)                             # by hand...
    p.check_auto(23 * 60)
    assert p.bank == "day"                                        # ...lasts till the next switch time
    p.check_auto(7 * 60)
    p.check_auto(21 * 60)
    assert p.bank == "night"
    for bad in ({"on": "yes"}, {"night_min": 2000}, {"night_min": 60, "day_min": 60}):
        try:
            pm.validate_auto(bad)
            assert False, bad
        except ValueError:
            pass


def test_the_dj_can_be_off(tmp_path) -> None:
    from test_artist_radio import _station
    from sleepradiopi.broadcast.models import LinkKind
    st = _station(tmp_path)
    st._say = lambda text, *a: text
    track = st._take_next()
    assert any(s.kind == "say" for s in st._build_gap(LinkKind.LINK, False, [], track, st._take_next()))
    st.set_dj(dj_on=False)
    assert st.dj_settings()["dj_on"] is False
    assert not any(s.kind == "say" for s in st._build_gap(LinkKind.LINK, False, [], track, st._take_next()))
    assert not any(s.kind == "say" for s in st._build_gap(LinkKind.TIME_CHECK, False, [], track, st._take_next()))
    msg = st._build_gap(LinkKind.LINK, False, [], track, st._take_next(), "A message for Dad.")
    assert not any(s.kind == "say" for s in msg)                  # no speech at all: messages too
    # lines planned before the DJ went off aren't said either
    said = []
    st._speak = lambda speech, *a, **k: said.append(speech)
    from sleepradiopi.broadcast.station import Step
    st._run_steps([Step("say", "Coming up next."), Step("clock"), Step("news")])
    assert said == []
    st._prepare_opening()
    assert not any(s.kind == "say" for s in st._opening[1])       # no welcome either


def test_with_the_dj_off_the_buttons_beep_instead_of_talking(tmp_path) -> None:
    from sleepradiopi.io import presets as pm

    class Station:
        artist = profile = source = None
        _has_voice = True
        dj_on = False
        rendered = []
        def render_speech(self, text):
            self.rendered.append(text)
            import numpy as np
            return np.zeros((10, 2), np.int16)

    class Control:
        clips = []
        def play_clip(self, clip):
            self.clips.append(clip)
    st, c = Station(), Control()
    p = pm.Presets(st, c, None, [])
    p.press(1)                                        # an empty button
    import time
    time.sleep(0.2)
    assert st.rendered == [] and len(c.clips) == 1    # a beep only, no words


def test_the_cathedral_selector_plays_where_it_settles(monkeypatch) -> None:
    import time
    from sleepradiopi.io import presets as pm
    monkeypatch.setattr(pm, "SETTLE_S", 0.1)
    played = []

    class Station:
        artist = profile = source = None
        _has_voice = False
        def set_artist(self, a): played.append(("show", a))
        def set_profile(self, p): played.append(("list", p))
        def tune(self, s): played.append(("tune", s["name"] if s else None))
        dj_on = True
    p = pm.Presets(Station(), None, None,
                   [{"kind": "radio", "name": f"Station {i}", "url": f"http://s/{i}"} for i in range(6)],
                   count=6, selector=True)
    assert p.status()["count"] == 6 and p.status()["selector"]
    for i in (0, 1, 2):                 # turning from 1 to 4 passes 2 and 3...
        p.selector_down(i); time.sleep(0.03); p.selector_up(i)
    p.selector_down(3)                  # ...and stops on 4
    time.sleep(0.3)
    assert played == [("tune", "Station 3")]
    p.hold(3)                           # a switch has no hold-to-save
    assert p.presets[3]["name"] == "Station 3"


def test_the_back_button_swaps_day_and_night_or_confirms_the_menu() -> None:
    from sleepradiopi.io import presets as pm

    class Station:
        artist = profile = source = None
        _has_voice = False
    p = pm.Presets(Station(), None, None, [], count=6, selector=True)
    p.back_press()
    assert p.bank == "night"

    class Menu:
        active = False
        calls = []
        def open(self): self.calls.append("open"); self.active = True
        def confirm(self): self.calls.append("confirm")
        def select(self, i): self.calls.append(("select", i))
    p.menu = m = Menu()
    p.back_hold()
    p.press(2)
    p.back_press()
    assert m.calls == ["open", ("select", 2), "confirm"] and p.bank == "night"


def test_programming_the_other_set_without_swapping(tmp_path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [None] * 4)
    p.set(0, {"kind": "action", "action": "sleep_15"}, bank="night")
    assert p.bank == "day" and p.banks["night"][0] == {"kind": "action", "action": "sleep_15"} and p.banks["day"][0] is None
    assert p.status()["sets"]["night"][0]["label"] == "Sleep in 15 minutes"


def test_instant_does_it_now_without_a_button(tmp_path: Path) -> None:
    """The desktop's double-click on a jingle or an action (or a drop on the radio)."""
    st, ctl, conf, p = _presets(tmp_path)
    played, did = [], []
    p.jingle = played.append
    p._action = did.append
    assert p.instant({"kind": "jingle", "path": "Station ID.mp3"})["kind"] == "jingle"
    p.instant({"kind": "action", "action": "time"})
    assert played == ["Station ID.mp3"] and did == ["time"] and ctl.calls == []
    for bad in (None, {"kind": "show"}, {"kind": "jingle", "path": "../x.mp3"}):
        with pytest.raises(ValueError):
            p.instant(bad)


def test_a_colour_of_noise_on_a_button(tmp_path: Path) -> None:
    brown = {"kind": "noise", "colour": "brown"}
    assert presets_mod.validate(brown | {"x": 1}) == brown and presets_mod.label(brown) == "Brown noise"
    with pytest.raises(ValueError):
        presets_mod.validate({"kind": "noise", "colour": "beige"})
    st, ctl, conf, p = _presets(tmp_path, [brown, {"kind": "noise", "colour": "pink"}])
    p.press(0)
    p.press(1)                                       # another colour: that one, still on
    p.press(1)                                       # the one that's on: off
    assert ctl.calls == ["noise on brown", "noise on pink", "noise off pink"]


def test_the_knob_held_and_what_plays_at_switch_on(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path)
    said = []

    class Announcer:
        def speak(self): said.append("address"); return True
    p.announcer = Announcer()
    assert p.knob_long() and said == ["address"]                 # nothing on it: the address, as it came
    assert not p.power_on() and st.source is None
    p.set_slot("knob_long", {"kind": "action", "action": "sleep_45"})
    p.set_slot("power_on", RP)
    assert load(conf).knob_long == {"kind": "action", "action": "sleep_45"} and load(conf).power_on == RP
    assert p.knob_long() and ctl.sleep == 45 and said == ["address"]
    p.hotspot = lambda: {"ssid": "SleepRadio"}                   # its own network: the address, so it can be found
    assert p.knob_long() and said == ["address"] * 2
    assert p.power_on() and st.source["url"] == "http://rp/"
    assert p.status()["slots"]["power_on"]["label"] == "Radio Paradise"
    with pytest.raises(ValueError, match="plays something"):
        p.set_slot("power_on", {"kind": "action", "action": "time"})
    with pytest.raises(ValueError):
        p.set_slot("doorbell", RP)
    p.set_slot("power_on", None)
    assert not p.power_on() and load(conf).power_on is None
    saved = backup.export(conf, None)
    saved["settings"]["power_on"] = {"kind": "action", "action": "time"}
    with pytest.raises(backup.BadSettings, match="power_on"):
        backup.parse(saved)
    p2 = Presets(st, ctl, conf, slots={"knob_long": {"kind": "tape"}, "power_on": RP})   # (a hand-edited config)
    assert p2.slots == {"knob_long": None, "power_on": RP}


def test_a_programme_can_put_the_night_buttons_on_for_a_stretch(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [None, None, None, {"kind": "action", "action": "bank"}])
    assert programmes.validate([{"name": "Night", "blocks": [], "switches": [{"what": "night", "from": 0, "min": 600}]}])
    p.scheduled("night_on")
    assert p.bank == "night" and ctl.clips == []                 # (quietly: it may be the middle of the night)
    p.scheduled("night_off")
    assert p.bank == "day"
    p.press(3)                                                   # on a button: swaps, with its three notes
    assert p.bank == "night" and ctl.clips == ["Button"]


def test_saved_speaker_sounds_are_checked() -> None:
    from sleepradiopi.audio.eq import validate_sounds
    assert validate_sounds([{"name": " Bass  port ", "eq": {"bass": 4}, "highpass": 140}]) == \
        [{"name": "Bass port", "eq": {"bass": 4, "mid": 0, "treble": 0}, "highpass": 140, "mono": False}]
    for bad in ([{"name": ""}], [{"name": "A"}, {"name": "a"}], [{"name": "A", "eq": {"bass": 99}}],
                [{"name": "A", "highpass": 999}], [{"name": "A", "mono": "yes"}], "x", [{"name": "A"}] * 21):
        with pytest.raises(ValueError):
            validate_sounds(bad)
