import io
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi import media as media_mod
from sleepradiopi.media import MediaError, MediaLibrary
from sleepradiopi.web.server import make_handler

from test_offline import _station


def _lib(tmp_path: Path, changed=None) -> MediaLibrary:
    music, jingles = tmp_path / "music", tmp_path / "jingles"
    (music / "ABBA" / "Gold" / "Disc 1").mkdir(parents=True)
    (music / "ABBA" / "Gold" / "Disc 1" / "01 - Dancing Queen.mp3").write_bytes(b"x" * 10)
    (music / "ABBA" / "Gold" / "cover.jpg").write_bytes(b"j")
    jingles.mkdir()
    return MediaLibrary({"music": music, "jingles": jingles}, on_changed=changed,
                        lease=tmp_path / "run" / "media-rw")


def _up(lib, kind, folder, name, data: bytes):
    return lib.receive(kind, folder, name, len(data), io.BytesIO(data).read)


def test_paths_stay_inside_the_library(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    for bad in ("../etc", "ABBA/../../x", ".hidden", "ABBA/.incoming", "a\0b"):
        with pytest.raises(MediaError):
            lib.resolve("music", bad)
    with pytest.raises(MediaError):
        lib.resolve("videos", "")
    (tmp_path / "outside").mkdir()
    (tmp_path / "music" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(MediaError, match="inside"):
        lib.resolve("music", "link")
    assert lib.resolve("music", "ABBA/Gold") == tmp_path / "music" / "ABBA" / "Gold"


def test_listing(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    top = lib.list("music", "")
    assert top["folders"] == [{"name": "ABBA", "items": 1}] and top["files"] == []
    gold = lib.list("music", "ABBA/Gold")
    assert gold["path"] == "ABBA/Gold" and gold["folders"] == [{"name": "Disc 1", "items": 1}]
    assert gold["files"] == [{"name": "cover.jpg", "size": 1, "audio": False}]
    assert gold["usage"]["free"] > 0 and gold["writable"]
    with pytest.raises(MediaError):
        lib.list("music", "Nobody")


def test_uploads(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    assert _up(lib, "music", "", "Queen/Greatest Hits/01 - Bohemian Rhapsody.mp3", b"song") == \
        {"path": "Queen/Greatest Hits/01 - Bohemian Rhapsody.mp3", "status": "added"}
    assert (tmp_path / "music" / "Queen" / "Greatest Hits" / "01 - Bohemian Rhapsody.mp3").read_bytes() == b"song"
    # the same file again is skipped; a different one of the same name kept alongside
    assert _up(lib, "music", "Queen/Greatest Hits", "01 - Bohemian Rhapsody.mp3", b"song")["status"] == "same"
    assert _up(lib, "music", "Queen/Greatest Hits", "01 - Bohemian Rhapsody.mp3", b"other")["path"] == \
        "Queen/Greatest Hits/01 - Bohemian Rhapsody (2).mp3"
    assert _up(lib, "music", "ABBA/Gold", "folder.png", b"img")["status"] == "added"    # cover art
    assert _up(lib, "jingles", "", "Station ID.mp3", b"ding")["path"] == "Station ID.mp3"
    for kind, name in (("music", "notes.txt"), ("jingles", "cover.jpg"), ("music", "../x.mp3")):
        with pytest.raises(MediaError):
            _up(lib, kind, "", name, b"x")
    assert not list((tmp_path / "music" / media_mod.INCOMING).iterdir())    # no .part files left


def test_a_cut_short_upload_leaves_nothing(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(MediaError, match="cut short"):
        lib.receive("music", "", "Half.mp3", 100, io.BytesIO(b"only fifty" * 5).read)
    assert not (tmp_path / "music" / "Half.mp3").exists()
    assert not list((tmp_path / "music" / media_mod.INCOMING).iterdir())


def test_room_and_size_limits(tmp_path: Path, monkeypatch) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(MediaError, match="GB"):
        lib.receive("music", "", "big.flac", media_mod.MAX_FILE + 1, io.BytesIO(b"").read)
    monkeypatch.setattr(lib, "usage", lambda: {"total": 0, "used": 0, "free": media_mod.KEEP_FREE + 10})
    with pytest.raises(MediaError, match="room"):
        _up(lib, "music", "", "a.mp3", b"x" * 100)


def test_folders_and_deleting(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    assert lib.mkdir("music", "", "Queen") == "Queen"
    assert (tmp_path / "music" / "Queen").is_dir()
    lib.delete("music", "ABBA/Gold/Disc 1/01 - Dancing Queen.mp3")
    assert not (tmp_path / "music" / "ABBA" / "Gold" / "Disc 1" / "01 - Dancing Queen.mp3").exists()
    lib.delete("music", "ABBA")
    assert not (tmp_path / "music" / "ABBA").exists()
    for bad in ("", "Nobody"):
        with pytest.raises(MediaError):
            lib.delete("music", bad)


def test_the_lease_and_the_rescan(tmp_path: Path) -> None:
    rescans = []
    lib = _lib(tmp_path, lambda: rescans.append(1))
    assert not lib.done() and rescans == []                  # nothing changed: no rescan
    _up(lib, "music", "", "a.mp3", b"x")
    assert (tmp_path / "run" / "media-rw").read_text().isdigit()   # asked for /media to be writable
    assert lib.done() and rescans == [1]
    assert not (tmp_path / "run" / "media-rw").exists()      # ...and let it go


def test_waits_for_the_helper_then_gives_up(tmp_path: Path, monkeypatch) -> None:
    lib = _lib(tmp_path)
    monkeypatch.setattr(media_mod, "WAIT_WRITABLE_S", 0.3)
    monkeypatch.setattr(MediaLibrary, "_writable", staticmethod(lambda root: False))
    with pytest.raises(MediaError, match="writable"):
        lib.delete("music", "ABBA")
    assert (tmp_path / "music" / "ABBA").exists()


def test_the_station_rescans_and_forgets_deleted_songs(tmp_path: Path) -> None:
    st = _station(tmp_path)                                   # Artist/Album/01..03
    folder = tmp_path / "music" / "Artist" / "Album"
    st._refill()
    queued = list(st._queue)
    (folder / "04 - Song 4.mp3").write_bytes(b"x")
    gone = queued[0].path
    gone.unlink()
    assert st.reload_library()["tracks"] == 3
    assert all(t.path != gone for t in st._queue)
    assert {t.path.name for t in st.tracks} == {"01 - Song 1.mp3", "02 - Song 2.mp3", "03 - Song 3.mp3",
                                                 "04 - Song 4.mp3"} - {gone.name}
    assert st.albums()[0]["tracks"][-1].path.name == "04 - Song 4.mp3"   # album index rebuilt


def test_web_api(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    st = _station(tmp_path / "st")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, media=lib))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(base + path, data=data, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, d = call("/api/media?kind=music&path=ABBA")
        assert code == 200 and d["folders"][0]["name"] == "Gold"
        code, d = call("/api/media/upload?kind=music&dir=ABBA/Gold&name=" +
                       urllib.request.quote("Disc 2/01 - Waterloo.mp3"), raw=b"waterloo")
        assert code == 200 and d == {"path": "ABBA/Gold/Disc 2/01 - Waterloo.mp3", "status": "added"}
        code, d = call("/api/media/upload?kind=music&dir=&name=evil.sh", raw=b"rm -rf")
        assert code == 400 and "plays" in d["error"]
        code, d = call("/api/media/mkdir", {"kind": "music", "path": "", "name": "Queen"})
        assert code == 200 and d["path"] == "Queen"
        code, d = call("/api/media/delete", {"kind": "music", "path": "../../etc"})
        assert code == 400
        code, d = call("/api/media/delete", {"kind": "music", "path": "ABBA/Gold/Disc 1"})
        assert code == 200
        code, d = call("/api/media/done", {})
        assert code == 200 and d["changed"] and "tracks" in d["library"]
    finally:
        httpd.shutdown()
