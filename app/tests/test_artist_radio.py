import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.broadcast.script_builder import DjScriptBuilder, artist_station_name
from sleepradiopi.broadcast.station import artist_key
from sleepradiopi.config.settings import Settings, load
from sleepradiopi.web.server import make_handler

from test_offline import FakeTts, NullOutput

LIBRARY = {
    "The Beatles/Rubber Soul": ["Girl", "Michelle", "The Word", "Nowhere Man"],
    "Crowded House/Woodface": ["Weather With You", "Fall at Your Feet", "Four Seasons in One Day"],
    "Nick Drake/Pink Moon": ["Pink Moon", "Place to Be"],
}


def _station(tmp_path: Path, **cfg_extra) -> station_mod.Station:
    for folder, songs in LIBRARY.items():
        d = tmp_path / "music" / folder
        d.mkdir(parents=True)
        for n, song in enumerate(songs, 1):
            (d / f"0{n} - {song}.mp3").write_bytes(b"x")
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "none",
               hooks_file="", scan_cache=tmp_path / "scans.json", tag_cache=None)
    cfg.update(cfg_extra)
    return station_mod.Station(cfg, FakeTts(), NullOutput())


def test_names() -> None:
    assert artist_station_name("The Beatles") == "Beatles Radio"
    assert artist_station_name("Crowded House") == "Crowded House Radio"
    assert artist_station_name(None) == "Sleep Radio"
    assert artist_key("The  Beatles ") == artist_key("beatles") == "beatles"
    b = DjScriptBuilder(station="Beatles Radio")
    assert b.welcome_greeting(time_known=False) == "Hello, and welcome to Beatles Radio."
    assert all("Sleep Radio" not in b._pick([i]) for i in ("You're listening to {station}.",))


def test_artists_are_listed_with_their_track_counts(tmp_path: Path) -> None:
    st = _station(tmp_path)
    names = {a["name"]: a["tracks"] for a in st.artists()}
    assert names == {"The Beatles": 4, "Crowded House": 3, "Nick Drake": 2}


def test_an_artist_plays_as_a_one_artist_theme(tmp_path: Path) -> None:
    """Artist radio is retired: an artist is a theme of their own, made the first
    time (still announced as artist radio was)."""
    st = _station(tmp_path)
    assert st.set_artist("Crowded House")
    assert st.builder.station == "Crowded House Radio" and st.profile == "Crowded House" and st.artist is None
    assert [(p["name"], p["artists"]) for p in st.profiles] == [("Crowded House", ["Crowded House"])]
    picks = [st._take_next() for _ in range(12)]        # off air: the opening was rebuilt too
    assert {t.artist for t in picks} == {"Crowded House"}
    assert st._opening[2].artist == "Crowded House"
    assert "welcome to Crowded House Radio" in st._opening[0]
    assert st.set_artist("the beatles") and st.profile == "Beatles"           # ("The" dropped, as before)
    assert st.set_artist("Crowded House") and len(st.profiles) == 2           # (found again, not made twice)


def test_on_air_another_station_takes_over_at_once(tmp_path: Path, monkeypatch) -> None:
    """Like turning the dial: the song playing stops and the new station opens
    with its own music (it used to wait for the song's end, and a theme dropped
    on the radio seemed to do nothing)."""
    from collections import deque
    st = _station(tmp_path)
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st._in_music = True                      # the music show is what's on
    other = next(t for t in st.tracks if t.artist != "The Beatles")
    st._queue = deque([other])               # lined up next: not the Beatles
    st._in_gap = True                        # (even with the DJ announcing it)
    st.set_artist("beatles")
    assert st._switch.is_set() and st._retune                # the show opens it now
    assert list(st._queue) == [] and st._opening is None     # nothing of the old station's kept
    st._switch.clear()
    st.set_artist("beatles")                                 # the same station: nothing happens
    assert not st._switch.is_set()


def test_editing_the_theme_playing_doesnt_cut_in(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st.set_profiles([{"name": "Default", "all": True}, {"name": "Fab", "artists": ["The Beatles"]}])
    st.set_profile("Fab")
    st._in_music, st._retune = True, False
    st._switch.clear()
    st.set_profiles([{"name": "Default", "all": True}, {"name": "Fab", "artists": ["The Beatles", "Nick Drake"]}])
    assert not st._switch.is_set() and not st._retune


def test_the_show_loop_opens_the_new_station(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    monkeypatch.setattr(st.output, "start", lambda: None, raising=False)
    monkeypatch.setattr(st.output, "stop", lambda: None, raising=False)
    runs = []

    def run_music(back=False):
        runs.append((back, st.profile))
        if len(runs) == 1:
            st._in_music = True
            st.set_artist("beatles")             # chosen mid-show
            st._in_music = False
        else:
            st._stop.set()
    monkeypatch.setattr(st, "_run_music", run_music)
    st._run_show()
    assert runs[0] == (False, None) and runs[1] == (True, "Beatles")   # opened as from elsewhere


def test_unknown_artist_falls_back_to_everything(tmp_path: Path) -> None:
    st = _station(tmp_path)
    assert not st.set_artist("Nobody")
    assert st.artist is None and st.builder.station == "Sleep Radio"


def test_an_artist_saved_from_before_becomes_a_theme_at_start_up(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from sleepradiopi.io.presets import Presets
    from sleepradiopi.main import retire_artist_radio
    st = _station(tmp_path, broadcast_artist="Nick Drake")
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"broadcast_artist": "Nick Drake"}))
    presets = Presets(st, None, conf, [{"kind": "show", "artist": "The Beatles"}, {"kind": "show", "artist": "Nobody"}])
    assert retire_artist_radio(st, conf, presets, SimpleNamespace(broadcast_artist="Nick Drake", broadcast_profile=None))
    assert st.profile == "Nick Drake" and st.builder.station == "Nick Drake Radio"
    assert [p["profile"] for p in presets.banks["day"][:2]] == ["Beatles", "Default"]   # (gone: Default)
    saved = load(conf)
    assert saved.broadcast_artist is None and saved.broadcast_profile == "Nick Drake"
    assert [p["name"] for p in saved.profiles] == ["Nick Drake", "Beatles"]


def test_web_api_lists_and_sets_the_artist(tmp_path: Path) -> None:
    st = _station(tmp_path)
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music"}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/station", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    try:
        with urllib.request.urlopen(base + "/api/artists") as r:
            data = json.load(r)
        assert [a["name"] for a in data["artists"]] == ["The Beatles", "Crowded House", "Nick Drake"]
        with pytest.raises(urllib.error.HTTPError) as err:
            post({"artist": "The Beatles"})                   # artist radio is retired
        assert err.value.code == 400
        st.set_profiles([{"name": "Fab", "artists": ["The Beatles"]}])
        assert post({"profile": "Fab"}) == {"found": True, "artist": None, "profile": "Fab", "station_name": "Fab Radio"}
        assert load(conf).broadcast_profile == "Fab" and load(conf).broadcast_artist is None
        assert json.loads(conf.read_text())["music_folder"] == "/media/music"
        assert post({"artist": None})["profile"] == "Fab"     # (nothing chosen: the theme last played)
        with pytest.raises(urllib.error.HTTPError) as err:
            post({"profile": 5})
        assert err.value.code == 400
    finally:
        httpd.shutdown()


def test_choosing_the_show_that_is_on_keeps_its_welcome(tmp_path) -> None:
    st = _station(tmp_path)
    st.set_artist("The Beatles")
    opening = st._opening
    assert opening is not None
    assert st.set_artist("the beatles")               # the same: nothing thrown away
    assert st._opening is opening
    st.set_artist("Nick Drake")                        # a different choice: a new welcome
    assert st._opening is not opening
