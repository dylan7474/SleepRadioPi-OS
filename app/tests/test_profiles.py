import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast.profiles import station_name, validate
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station

FRIDAY = {"name": "Friday List", "artists": ["The Beatles", "Crowded House"]}


def test_validate_and_names() -> None:
    got = validate([{"name": "  Friday   List ", "artists": ["crowded house", "The Beatles", "The Beatles"]}])
    assert got == [{"name": "Friday List", "artists": ["crowded house", "The Beatles"]}]
    assert station_name("Friday List") == "Friday List Radio"
    assert station_name("Carisbrooke") == "Carisbrooke Radio"
    assert station_name("Rock Radio") == "Rock Radio"
    for bad in ([{"name": "", "artists": ["A"]}], [{"name": "X", "artists": []}],
                [{"name": "X", "artists": ["A"]}, {"name": "x", "artists": ["B"]}], "nope",
                [{"name": "X", "artists": [3]}]):
        with pytest.raises(ValueError):
            validate(bad)


def test_a_profile_plays_only_its_artists(tmp_path: Path) -> None:
    st = _station(tmp_path, profiles=[FRIDAY])
    assert st.set_profile("friday list")                       # any case
    assert st.profile == "Friday List" and st.artist is None
    assert st.builder.station == "Friday List Radio"
    assert {st._take_next().artist for _ in range(20)} == {"The Beatles", "Crowded House"}
    assert "welcome to Friday List Radio" in st._opening[0]
    st.set_artist("Nick Drake")                                # choosing an artist replaces it
    assert st.profile is None and st.artist == "Nick Drake"


def test_editing_the_playing_profile(tmp_path: Path) -> None:
    st = _station(tmp_path, profiles=[FRIDAY])
    st.set_profile("Friday List")
    st.set_profiles([{"name": "Friday List", "artists": ["Nick Drake"]}])
    assert {st._take_next().artist for _ in range(6)} == {"Nick Drake"}
    st.set_profiles([])                                        # deleted: back to everything
    assert st.profile is None and st.builder.station == "Sleep Radio"


def test_unknown_or_empty_profile_plays_everything(tmp_path: Path) -> None:
    st = _station(tmp_path, profiles=[{"name": "Nobody", "artists": ["Not In The Library"]}])
    assert not st.set_profile("Nobody") and st.profile is None
    assert not st.set_profile("Missing") and st.builder.station == "Sleep Radio"


def test_saved_profile_is_used_at_start_up(tmp_path: Path) -> None:
    st = _station(tmp_path, profiles=[FRIDAY], broadcast_profile="Friday List")
    assert st.profile == "Friday List" and st._opening[2].artist in ("The Beatles", "Crowded House")


def test_bad_profiles_in_the_config_dont_stop_the_station(tmp_path: Path) -> None:
    st = _station(tmp_path, profiles=[{"name": "", "artists": []}], broadcast_profile="X")
    assert st.profiles == [] and st.profile is None


def test_backup_checks_profiles() -> None:
    good = {"format": backup.FORMAT, "version": 1, "settings": {"profiles": [FRIDAY],
                                                              "broadcast_profile": "Friday List"}}
    settings, _ = backup.parse(good)
    assert settings["profiles"][0]["name"] == "Friday List"
    with pytest.raises(backup.BadSettings):
        backup.parse({**good, "settings": {"profiles": [{"name": "X", "artists": []}]}})


def test_web_api(tmp_path: Path) -> None:
    st = _station(tmp_path)
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        assert req("/api/profiles", {"profiles": [FRIDAY]})["profiles"][0]["name"] == "Friday List"
        assert req("/api/artists")["profiles"] == st.profiles
        got = req("/api/station", {"profile": "Friday List"})
        assert got["found"] and got["station_name"] == "Friday List Radio"
        saved = load(conf)
        assert saved.broadcast_profile == "Friday List" and saved.profiles[0]["artists"]
        assert req("/api/profiles", {"profiles": []})["profile"] is None   # deleted while playing
        assert load(conf).broadcast_profile is None
        with pytest.raises(urllib.error.HTTPError) as err:
            req("/api/profiles", {"profiles": [{"name": "X", "artists": []}]})
        assert err.value.code == 400 and "at least one artist" in json.load(err.value)["error"]
    finally:
        httpd.shutdown()
