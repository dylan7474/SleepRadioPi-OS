"""The desktop's media manager: moving between folders and libraries (with
playlists, programmes and buttons following), downloads, and real lengths."""

import io
import json
import threading
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast.programmes import Scheduler
from sleepradiopi.media import MediaError, MediaLibrary
from sleepradiopi.web.server import make_handler

from test_ondemand import _od_station


def _lib(tmp_path):
    return MediaLibrary({"music": tmp_path / "music", "ondemand": tmp_path / "ondemand"}, lease=tmp_path / "lease")


def test_move_between_libraries_and_folders(tmp_path: Path) -> None:
    _od_station(tmp_path)
    lib = _lib(tmp_path)
    new = lib.move("music", "Nick Drake/Pink Moon", "ondemand", "Thunderstorms")
    assert new == "Thunderstorms/Pink Moon"
    assert (tmp_path / "ondemand/Thunderstorms/Pink Moon/01 - Pink Moon.mp3").is_file()
    assert not (tmp_path / "music/Nick Drake/Pink Moon").exists()
    assert lib.move("ondemand", "Thunderstorms/Pink Moon", "music", "") == "Pink Moon"
    for bad in (("music", "", "ondemand", ""), ("music", "Nobody", "ondemand", ""), ("music", "The Beatles", "music", "The Beatles/Rubber Soul"),
                ("music", "Pink Moon", "ondemand", "No such folder")):
        with pytest.raises(MediaError):
            lib.move(*bad)
    lib.done()


def test_playlists_programmes_and_buttons_follow_a_move(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    st.add_to_playlist("Sunday", [["music", "The Beatles/Rubber Soul/02 - Michelle.mp3"], ["music", "Nick Drake/Pink Moon/01 - Pink Moon.mp3"]])
    assert st.rename_refs("music", "The Beatles", "ondemand", "Old/The Beatles")
    assert st.playlists[0]["tracks"] == [["ondemand", "Old/The Beatles/Rubber Soul/02 - Michelle.mp3"], ["music", "Nick Drake/Pink Moon/01 - Pink Moon.mp3"]]
    assert not st.rename_refs("music", "The Beat", "ondemand", "x")           # a prefix of a name isn't the folder
    sched = Scheduler(st)
    sched.set_programmes([{"name": "P", "blocks": [{"name": "B", "items": [{"kind": "album", "root": "music", "folder": "Nick Drake/Pink Moon"}]}]}])
    assert sched.rename_refs("music", "Nick Drake", "ondemand", "Nick Drake")
    assert sched.programmes[0]["blocks"][0]["items"][0] == {"kind": "album", "root": "ondemand", "folder": "Nick Drake/Pink Moon"}


def test_lengths(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    monkeypatch.setattr(type(st), "track_seconds", lambda self, t: 180.0 if "Michelle" not in t.path.name else None)
    assert st.minutes_of({"kind": "track", "root": "music", "path": "Nick Drake/Pink Moon/01 - Pink Moon.mp3"}) == 3.0
    assert st.minutes_of({"kind": "album", "root": "music", "folder": "The Beatles/Rubber Soul"}) == 12.0   # one unreadable: the average
    assert st.minutes_of({"kind": "album", "root": "ondemand", "folder": "Old radio shows", "deep": True}) == 9.0
    assert st.minutes_of({"kind": "station"}) is None


def test_web_move_download_and_lengths(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    st.add_to_playlist("Sunday", [["music", "Nick Drake/Pink Moon/01 - Pink Moon.mp3"]])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None, media=_lib(tmp_path)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None, raw=False):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return resp.read() if raw else json.load(resp)
    try:
        got = req("/api/media/move", {"kind": "music", "path": "Nick Drake", "to_kind": "ondemand", "to": ""})
        assert got == {"path": "Nick Drake", "kind": "ondemand"}
        assert st.playlists[0]["tracks"] == [["ondemand", "Nick Drake/Pink Moon/01 - Pink Moon.mp3"]]
        assert req("/api/media/download?kind=ondemand&path=Nick%20Drake/Pink%20Moon/01%20-%20Pink%20Moon.mp3", raw=True) == b"x"
        z = zipfile.ZipFile(io.BytesIO(req("/api/media/download?kind=music&path=The%20Beatles", raw=True)))
        assert sorted(z.namelist())[0] == "The Beatles/Rubber Soul/01 - Girl.mp3" and len(z.namelist()) == 4
        assert req("/api/lengths", {"items": [{"kind": "station"}]}) == {"minutes": [None]}
    finally:
        httpd.shutdown()
