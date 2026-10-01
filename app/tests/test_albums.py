import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import Future
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast.models import LinkKind
from sleepradiopi.broadcast.script_builder import DjScriptBuilder, album_phrase
from sleepradiopi.broadcast.station import Speech, _natural
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station


def _speech(text, *a):
    f = Future()
    f.set_result(None)
    return Speech(text, "voice", f)


def _album_id(st, title):
    return next(a["id"] for a in st.albums() if a["title"] == title)


def _on_air(st, monkeypatch):
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(st, "_say", _speech)


def test_albums_are_folders_in_track_order(tmp_path: Path) -> None:
    st = _station(tmp_path)
    rubber = next(a for a in st.albums() if a["title"] == "Rubber Soul")
    assert rubber["artist"] == "The Beatles"
    assert [t.title for t in rubber["tracks"]] == ["Girl", "Michelle", "The Word", "Nowhere Man"]
    assert sorted(["10 - b", "9 - a", "1 - c"], key=_natural) == ["1 - c", "9 - a", "10 - b"]
    assert [a["title"] for a in st.search_albums("beatles")] == ["Rubber Soul"]
    assert st.search_albums("woodface")[0]["tracks"] == 3


def test_album_phrases() -> None:
    b = DjScriptBuilder()
    album = {"title": "Rubber Soul", "artist": "The Beatles"}
    assert album_phrase(album) == "Rubber Soul, by The Beatles"
    assert album_phrase({"title": "Now 42", "artist": "Various artists"}).endswith("by various artists")
    from sleepradiopi.broadcast.models import BroadcastTrack
    intro = b.album_intro(album, BroadcastTrack(Path("x"), "Girl", "The Beatles"))
    assert "Rubber Soul, by The Beatles" in intro and "Girl" in intro
    assert "Rubber Soul" in b.album_outro(album)


def test_an_album_plays_straight_through_with_an_intro_and_outro(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    current = st._take_next()
    st._refill()
    st._plan = st._plan_gap(current, st._queue[0])     # a normal gap was planned
    st.request_album(_album_id(st, "Rubber Soul"))
    talk = " ".join(s.speech.text for s in st._plan if s.kind == "say")
    assert "album" in talk and "Rubber Soul, by The Beatles" in talk   # re-worded to introduce it
    played, gaps = [], []
    prev = st._take_next()
    for _ in range(3):
        st._track_started(prev)
        nxt = st._queue[0]
        gaps.append(st._plan_gap(prev, nxt))
        played.append(prev.title)
        prev = st._take_next()
    played.append(prev.title)
    st._track_started(prev)
    assert played == ["Girl", "Michelle", "The Word", "Nowhere Man"]
    assert gaps == [[], [], []]                        # nothing between album tracks
    assert st.album_status() is not None
    st._refill()
    last_gap = st._plan_gap(prev, st._queue[0])
    assert "Rubber Soul" in last_gap[0].speech.text    # "That was Rubber Soul…"
    assert st._albums == [] and st.album_status() is None


def test_the_song_playing_being_on_the_album_doesnt_confuse_it(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    album = next(a for a in st.albums() if a["title"] == "Rubber Soul")
    last = album["tracks"][-1]
    st._track_started(last)                             # Nowhere Man is on air already
    st.request_album(album["id"])
    plan = st._plan_gap(last, st._queue[0])
    assert any("Rubber Soul" in s.speech.text and "album" in s.speech.text for s in plan if s.kind == "say")
    assert len(st._albums) == 1                         # not taken as the album's end


def test_stop_album_drops_the_rest(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    st.current_track = st._take_next()
    st.request_album(_album_id(st, "Woodface"))
    st.request(next(i for i, t in enumerate(st.tracks) if t.title == "Pink Moon"))
    assert st.stop_album()
    assert [r["title"] for r in st.requests()] == ["Pink Moon"]   # other requests stay
    assert st._albums == [] and not st.stop_album()


def test_off_air_the_show_opens_with_the_album(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    monkeypatch.setattr(st, "_say", _speech)
    st.request_album(_album_id(st, "Pink Moon"))
    assert st._opening[2].title == "Pink Moon"
    assert "an album" in st._opening[1][0].speech.text or "album" in st._opening[1][-1].speech.text


def test_news_waits_until_the_album_is_over(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path)
    _on_air(st, monkeypatch)
    st.request_album(_album_id(st, "Rubber Soul"))
    first = st._take_next()
    st._track_started(first)
    st._plan_gap(first, st._queue[0])
    assert st._gap_is_album
    ran = []
    monkeypatch.setattr(st, "_run_steps", lambda steps, gap=False: ran.append(steps))
    st._news_ready = object()                          # a bulletin that would be due
    monkeypatch.setattr(st.news_schedule, "due_at", lambda now: (_ for _ in ()).throw(AssertionError("checked news")))
    st._run_gap()
    assert ran == [[]]


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
        found = req("/api/search?q=rubber")
        assert found["albums"][0]["title"] == "Rubber Soul" and found["albums"][0]["tracks"] == 4
        got = req("/api/album", {"id": found["albums"][0]["id"]})
        assert got["tracks"] == 4 and got["album"]["title"] == "Rubber Soul"
        assert req("/api/album/stop", {})["stopped"] is True
        with pytest.raises(urllib.error.HTTPError) as err:
            req("/api/album", {"id": 999})
        assert err.value.code == 400
    finally:
        httpd.shutdown()
