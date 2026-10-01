import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast.models import JingleClip, LinkKind
from concurrent.futures import Future

from sleepradiopi.broadcast.station import ClockStep, Speech, Step
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station


def _speech(text, *a):
    f = Future()
    f.set_result(None)
    return Speech(text, "voice", f)


def _on_air(st, monkeypatch):
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st._in_music = True                      # the music show is what's on
    monkeypatch.setattr(st, "_say", _speech)


def _id(st, title):
    return next(i for i, t in enumerate(st.tracks) if t.title == title)


def test_search_matches_every_word_across_title_artist_album(tmp_path: Path) -> None:
    st = _station(tmp_path)
    assert [r["title"] for r in st.search("girl")] == ["Girl"]
    assert [r["title"] for r in st.search("beatles word")] == ["The Word"]
    assert {r["artist"] for r in st.search("woodface")} == {"Crowded House"}
    assert st.search("   ") == [] and st.search("zzz") == []


def test_off_air_the_show_opens_with_the_request(tmp_path: Path) -> None:
    st = _station(tmp_path)
    st.request(_id(st, "Pink Moon"))
    assert st._opening[2].title == "Pink Moon"
    st.request(_id(st, "Girl"))                 # a second one plays after it
    assert st._opening[2].title == "Pink Moon"
    assert [r["title"] for r in st.requests()] == ["Pink Moon", "Girl"]
    assert st._take_next().title == "Girl"


def test_mid_song_the_gap_is_reworded_for_the_request(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    track = st._take_next()
    st._refill()
    clock = ClockStep()
    st._plan = [Step("say", _speech("That was A. Coming up, Old Next.")), Step("clock", clock=clock),
                Step("say", _speech("Here's Old Next."))]
    st._gap_decision = (LinkKind.TIME_CHECK, False, [], track, None)
    reply = st.request(_id(st, "Fall at Your Feet"))
    assert reply["replanned"] and st.next_track.title == "Fall at Your Feet"
    talk = " ".join(s.speech.text for s in st._plan if s.kind == "say")
    assert "Fall at Your Feet" in talk and "Old Next" not in talk
    assert any(s.clock is clock for s in st._plan if s.kind == "clock")   # the worded time is kept
    assert st._take_next().title == "Fall at Your Feet"
    assert st.requests() == []


def test_in_the_gap_it_plays_after_the_announced_song(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    st._refill()
    announced = st._queue[0]
    st._in_gap = True
    reply = st.request(_id(st, "Michelle"))
    assert reply["after_announced"] and not reply["replanned"]
    assert st._take_next() == announced          # what the DJ just introduced
    st._in_gap = False
    st._place_pending()
    assert st._take_next().title == "Michelle"


def test_requests_survive_a_change_of_artist(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    st.request(_id(st, "Pink Moon"))
    st.set_artist("Crowded House")
    assert st._take_next().title == "Pink Moon"  # asked for, so it still plays
    assert st._take_next().artist == "Crowded House"


def test_jingles_on_a_theme_or_artist_radio_too(tmp_path: Path, monkeypatch) -> None:
    """The user's radio for their dad plays a theme all day: jingles follow the
    jingles on/off setting everywhere, not only the main mix."""
    st = _station(tmp_path)
    monkeypatch.setattr(st, "_jingle_due", lambda: True)
    monkeypatch.setattr(st, "_say", _speech)
    track = st._take_next()
    assert any(s.kind == "jingle" for s in st._plan_gap(track, st._take_next()))
    st.set_artist("The Beatles")
    assert any(s.kind == "jingle" for s in st._plan_gap(track, st._take_next()))
    st.set_artist(None)
    st.set_profiles([{"name": "Friday List", "artists": ["Nick Drake"]}])
    st.set_profile("Friday List")
    assert any(s.kind == "jingle" for s in st._plan_gap(track, st._take_next()))
    monkeypatch.setattr(st, "_jingle_due", lambda: False)        # (jingles off: _jingle_due never is)
    assert not any(s.kind == "jingle" for s in st._plan_gap(track, st._take_next()))


def test_an_opening_jingle_on_a_theme_too(tmp_path: Path) -> None:
    st = _station(tmp_path)
    st.jingles = [JingleClip(Path("sleep-radio.mp3"), 4.0)]
    st._reselect(artist=None)                    # rebuilds the opening
    assert any(s.kind == "jingle" for s in st._opening[1])
    st.set_artist("The Beatles")
    assert any(s.kind == "jingle" for s in st._opening[1])


def test_web_api(tmp_path: Path) -> None:
    st = _station(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        results = req("/api/search?q=pink%20moon")["results"]
        assert [r["title"] for r in results] == ["Pink Moon", "Place to Be"]   # the album matches too
        got = req("/api/request", {"id": results[0]["id"]})
        assert got["title"] == "Pink Moon" and got["requests"] == [{"title": "Pink Moon", "artist": "Nick Drake", "album": False}]
        for bad in ({"id": 999}, {"id": "1"}, {}):
            with pytest.raises(urllib.error.HTTPError) as err:
                req("/api/request", bad)
            assert err.value.code == 400
    finally:
        httpd.shutdown()
