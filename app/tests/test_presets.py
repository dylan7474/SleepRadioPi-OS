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

    def play_file(path, on_air, near_end=None):
        titles.append(on_air.title)
        real_play(path, on_air, near_end)
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
    assert presets_mod.label({"kind": "action", "action": "sleep"}) == "Sleep timer (30 min)"
    assert presets_mod.label(None) == "Empty"


class Control:
    def __init__(self):
        self.calls, self.clips, self.sleep = [], [], 0

    def toggle(self): self.calls.append("toggle")
    def play(self): self.calls.append("play")
    def play_clip(self, clip): self.clips.append(clip.label)
    def set_sleep(self, m): self.sleep = m
    def status(self): return {"sleep_min": self.sleep or None}


def _presets(tmp_path, buttons=None):
    st = _station(tmp_path)
    st.tts = None                                    # beeps only: no speech threads
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    ctl = Control()
    return st, ctl, conf, Presets(st, ctl, conf, buttons)


def test_a_press_plays_the_preset_and_again_pauses(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [RP])
    p.press(0)
    assert st.source["url"] == "http://rp/" and ctl.calls == ["play"]
    p.press(0)                                       # already playing: pause/play
    assert ctl.calls == ["play", "toggle"]
    assert p.status()["buttons"][0]["playing"]


def test_show_and_album_presets(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [
        {"kind": "show", "artist": "Artist"},
        {"kind": "album", "folder": "Artist/Album", "title": "Album", "artist": "Artist"},
        {"kind": "album", "folder": "Gone/Away", "title": "Gone", "artist": "Away"}])
    st.tune(RP)
    p.press(0)
    assert st.source is None and st.artist == "Artist"
    assert load(conf).broadcast_artist == "Artist"
    p.press(1)
    assert st.source["kind"] == "album" and st.source["folder"] == "Artist/Album"
    before = st.source
    p.press(2)                                       # not in this library: says sorry, keeps playing
    assert st.source is before and ctl.clips == ["Button"]


def test_a_hold_keeps_whats_playing(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path)
    st.tune(RP)
    p.hold(2)
    assert p.presets[2] == RP and load(conf).buttons[2] == RP
    st.tune(None)
    st.set_artist("Artist")
    p.hold(0)
    assert p.presets[0] == {"kind": "show", "artist": "Artist", "profile": None}
    assert p.status()["buttons"][0]["label"] == "Artist Radio"
    assert ctl.clips == ["Button", "Button"]         # a beep each time


def test_empty_buttons_and_actions(tmp_path: Path) -> None:
    st, ctl, conf, p = _presets(tmp_path, [None, {"kind": "action", "action": "sleep"},
                                           {"kind": "action", "action": "time"}])
    p.press(0)
    assert ctl.calls == [] and ctl.clips == ["Button"]
    p.press(1)
    assert ctl.sleep == presets_mod.SLEEP_MIN
    p.press(1)
    assert ctl.sleep == 0
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
        code, d = call("/api/buttons", {"button": 5, "preset": None})
        assert code == 400
        code, d = call("/api/buttons", {"button": 1, "preset": {"kind": "action", "action": "nope"}})
        assert code == 400
        code, d = call("/api/album/play", {"id": 0})
        assert code == 200 and d["source"]["kind"] == "album"
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
    assert p.bank == "night" and p.presets[0]["action"] == "sleep"
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
    p.hold(2)                                         # keeps what's playing on button 3...
    p.press(1)                                        # ...an empty button
    import time
    time.sleep(0.2)
    assert st.rendered == [] and len(c.clips) == 2    # beeps only, no words
    assert p.presets[2] is not None


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
    p.set(0, {"kind": "action", "action": "sleep"}, bank="night")
    assert p.bank == "day" and p.banks["night"][0] == {"kind": "action", "action": "sleep"} and p.banks["day"][0] is None
    assert p.status()["sets"]["night"][0]["label"] == "Sleep timer (30 min)"
