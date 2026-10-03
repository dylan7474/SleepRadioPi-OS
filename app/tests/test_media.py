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
    lib = _lib(tmp_path, lambda kinds: rescans.append(kinds))
    assert not lib.done() and rescans == []                  # nothing changed: no rescan
    _up(lib, "music", "", "a.mp3", b"x")
    assert (tmp_path / "run" / "media-rw").read_text().isdigit()   # asked for /media to be writable
    assert lib.done() and rescans == [{"music"}]
    _up(lib, "jingles", "", "j1.mp3", b"x")
    _up(lib, "jingles", "", "j2.mp3", b"x")
    assert lib.done() and rescans[-1] == {"jingles"}         # a jingle: only the jingles rescanned
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


def test_a_jingle_upload_rescans_only_the_jingles(tmp_path: Path, monkeypatch) -> None:
    from sleepradiopi.broadcast import station as station_mod
    st = _station(tmp_path)
    calls = []
    monkeypatch.setattr(station_mod, "scan_music", lambda *a: calls.append("music") or st.tracks)
    monkeypatch.setattr(station_mod, "scan_jingles", lambda *a: calls.append("jingles") or [])
    st.config.jingle_every = 4
    st.reload_library({"jingles"})
    assert set(calls) == {"jingles"}                             # (the station's and Power-on)
    calls.clear()
    st.reload_library()
    assert "music" in calls and "jingles" in calls                # (no kinds: everything)


def _sound(path: Path, codec: list[str], meta: dict | None = None) -> None:
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = [x for k, v in (meta or {}).items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", *tags, *codec, str(path)],
                   check=True)


def test_changing_tags(tmp_path: Path) -> None:
    """A track's tags are rewritten in a copy and swapped in; what they were first is kept, to put back."""
    od = tmp_path / "ondemand"
    planets = od / "Holst The Planets"
    _sound(planets / "01 - Mars.mp3", ["-c:a", "libmp3lame", "-b:a", "32k"], {"title": "Mars", "artist": "冨田勲", "album": "惑星", "track": "1"})
    _sound(planets / "02 - Venus.m4a", ["-c:a", "aac", "-b:a", "32k"], {"title": "Venus", "artist": "冨田勲"})
    _sound(planets / "03 - Bare.mp3", ["-c:a", "libmp3lame", "-b:a", "32k", "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0"])
    _sound(planets / "04 - Wave.wav", [])
    changed = []
    lib = MediaLibrary({"ondemand": od}, on_changed=changed.append, lease=tmp_path / "run" / "media-rw", tag_log=tmp_path / "tags.json")
    d = lib.tags("ondemand", "Holst The Planets")
    assert d["folder"] and d["name"] == "Holst The Planets" and [f["path"].split("/")[-1] for f in d["files"]] == \
        ["01 - Mars.mp3", "02 - Venus.m4a", "03 - Bare.mp3", "04 - Wave.wav"]
    mars = d["files"][0]
    assert (mars["title"], mars["artist"], mars["album"], mars["track"], mars["changed"]) == ("Mars", "冨田勲", "惑星", "1", False)
    assert d["files"][2]["title"] == "" and d["files"][3].get("fixed") is True

    size = (planets / "01 - Mars.mp3").stat().st_size
    for f in d["files"][:3]:                                 # mp3, m4a, and an mp3 that had no tags at all
        now = lib.retag("ondemand", f["path"], {"artist": "Isao Tomita", "album": " The  Planets "})
        assert now["artist"] == "Isao Tomita" and now["album"] == "The Planets" and now["title"] == f["title"]
    assert abs((planets / "01 - Mars.mp3").stat().st_size - size) < 4096 and not list((od / ".incoming").iterdir())
    assert lib.tags("ondemand", mars["path"])["files"][0]["changed"] is True
    assert json.loads((tmp_path / "tags.json").read_text())["ondemand/" + mars["path"]]["artist"] == "冨田勲"
    lib.retag("ondemand", mars["path"], {"title": "Mars, the Bringer of War", "track": ""})
    assert lib.tags("ondemand", mars["path"])["files"][0]["track"] == ""
    assert json.loads((tmp_path / "tags.json").read_text())["ondemand/" + mars["path"]]["title"] == "Mars"   # (still the first)
    lib.done()
    assert changed == [{"ondemand"}]

    new = lib.rename("ondemand", "Holst The Planets", "The Planets")      # renamed: what its tracks came with comes along
    assert lib.tags("ondemand", new)["files"][0]["changed"] is True
    assert lib.rename("ondemand", new, "Holst The Planets") == "Holst The Planets"
    back = lib.untag("ondemand", mars["path"])               # as it came
    assert (back["title"], back["artist"], back["album"], back["track"]) == ("Mars", "冨田勲", "惑星", "1")
    assert lib.tags("ondemand", mars["path"])["files"][0]["changed"] is False
    for bad, tags in ((d["files"][3]["path"], {"title": "x"}), (mars["path"], {}), (mars["path"], {"year": "1976"}),
                      (mars["path"], {"track": "three"}), (mars["path"], {"title": "x" * 300}), ("Nowhere.mp3", {"title": "x"})):
        with pytest.raises(MediaError):
            lib.retag("ondemand", bad, tags)
    with pytest.raises(MediaError):
        lib.untag("ondemand", d["files"][1]["path"] + "x")
    with pytest.raises(MediaError):
        lib.retag("jingles", "x.mp3", {"title": "x"})


def test_a_new_artist_tag_over_the_web_and_the_themes_follow(tmp_path: Path) -> None:
    """Themes choose songs by the artist's name: retagged songs stay in their theme, and the old name leaves it."""
    from dataclasses import asdict
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.config.settings import Settings, load
    from test_offline import FakeTts, NullOutput
    music = tmp_path / "music"
    for n in (1, 2):
        _sound(music / "The Beatles" / "Help" / f"0{n} - Song {n}.mp3", ["-c:a", "libmp3lame", "-b:a", "32k"],
               {"title": f"Song {n}", "artist": "The Beatles", "album": "Help"})
    _sound(music / "Nick Drake" / "Pink Moon" / "01 - Pink Moon.mp3", ["-c:a", "libmp3lame", "-b:a", "32k"],
           {"title": "Pink Moon", "artist": "Nick Drake"})
    cfg = asdict(Settings())
    cfg.update(music_folder=music, jingles_folder=tmp_path / "none", hooks_file="", scan_cache=tmp_path / "scans.json", tag_cache=None)
    st = station_mod.Station(cfg, FakeTts(), NullOutput())
    st.set_profiles([{"name": "Fab", "artists": ["The Beatles"]}])
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    lib = MediaLibrary({"music": music}, on_changed=st.reload_library, lease=tmp_path / "run" / "media-rw", tag_log=tmp_path / "tags.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf, media=lib))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    fab = lambda: set(next(p for p in st.profiles if p["name"] == "Fab")["artists"])
    one, two = "The Beatles/Help/01 - Song 1.mp3", "The Beatles/Help/02 - Song 2.mp3"
    try:
        code, d = call("/api/tags?kind=music&path=The%20Beatles")
        assert code == 200 and d["folder"] and [f["artist"] for f in d["files"]] == ["The Beatles"] * 2
        code, d = call("/api/tags", {"kind": "music", "path": one, "tags": {"artist": "The Fab Four"}})
        assert code == 200 and d["tags"]["artist"] == "The Fab Four" and fab() == {"The Beatles", "The Fab Four"}
        assert call("/api/media/done", {})[0] == 200
        assert fab() == {"The Beatles", "The Fab Four"}          # one song still has the old name
        assert {a["name"] for a in st.artists()} == {"The Beatles", "The Fab Four", "Nick Drake"}
        call("/api/tags", {"kind": "music", "path": two, "tags": {"artist": "The Fab Four"}})
        call("/api/media/done", {})
        assert fab() == {"The Fab Four"} and set(load(conf).profiles[0]["artists"]) == {"The Fab Four"}
        for p in (one, two):                                     # put back as they came: the theme follows them home
            assert call("/api/tags/restore", {"kind": "music", "path": p})[1]["tags"]["artist"] == "The Beatles"
        call("/api/media/done", {})
        assert fab() == {"The Beatles"}
        assert call("/api/tags", {"kind": "music", "path": one, "tags": {"year": "1965"}})[0] == 400
        # a folder renamed where it is: reported at its new place
        code, d = call("/api/media/rename", {"kind": "music", "path": "Nick Drake/Pink Moon", "name": "Pink Moon (1972)"})
        assert code == 200 and d["path"] == "Nick Drake/Pink Moon (1972)" and (music / "Nick Drake" / "Pink Moon (1972)").is_dir()
    finally:
        httpd.shutdown()
