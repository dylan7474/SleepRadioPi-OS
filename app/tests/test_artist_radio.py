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


def test_artist_radio_plays_only_that_artist(tmp_path: Path) -> None:
    st = _station(tmp_path)
    assert st.set_artist("Crowded House")
    assert st.builder.station == "Crowded House Radio" and st.artist == "Crowded House"
    picks = [st._take_next() for _ in range(12)]        # off air: the opening was rebuilt too
    assert {t.artist for t in picks} == {"Crowded House"}
    assert st._opening[2].artist == "Crowded House"
    assert "welcome to Crowded House Radio" in st._opening[0]
    st.set_artist(None)
    assert st.builder.station == "Sleep Radio" and st.artist is None
    assert len({t.artist for t in (st._take_next() for _ in range(30))}) == 3


def test_on_air_the_announced_next_track_still_plays(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st._in_music = True                      # the music show is what's on
    st._refill()
    announced = st._queue[0]
    st.set_artist("beatles")                            # any spelling
    assert st._take_next() == announced
    assert {st._take_next().artist for _ in range(8)} == {"The Beatles"}


def test_unknown_artist_falls_back_to_everything(tmp_path: Path) -> None:
    st = _station(tmp_path)
    assert not st.set_artist("Nobody")
    assert st.artist is None and st.builder.station == "Sleep Radio"


def test_the_saved_artist_is_used_at_start_up(tmp_path: Path) -> None:
    st = _station(tmp_path, broadcast_artist="Nick Drake")
    assert st.artist == "Nick Drake" and st.builder.station == "Nick Drake Radio"
    assert st._opening[2].artist == "Nick Drake"


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
        assert post({"artist": "The Beatles"}) == {"found": True, "artist": "The Beatles", "profile": None,
                                                   "station_name": "Beatles Radio"}
        assert load(conf).broadcast_artist == "The Beatles"
        assert json.loads(conf.read_text())["music_folder"] == "/media/music"
        assert post({"artist": "Nobody"})["found"] is False and load(conf).broadcast_artist is None
        assert post({"artist": None})["station_name"] == "Sleep Radio"
        with pytest.raises(urllib.error.HTTPError) as err:
            post({"artist": 5})
        assert err.value.code == 400
    finally:
        httpd.shutdown()
