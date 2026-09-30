"""On demand (/media/ondemand): played only when asked for, never in the show;
and the folder view of both libraries (Browse on the page)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi.io import presets
from sleepradiopi.media import MediaLibrary
from sleepradiopi.web.server import make_handler

from test_albums import _album_id, _on_air
from test_artist_radio import _station

ONDEMAND = {
    "Thunderstorms": ["Rain on a tin roof - 1 hour"],
    "Old radio shows/Hancock/Series 1": ["The First Night", "The Diary"],
    "Old radio shows/Hancock/Series 2": ["The Bequest"],
    "Classical/Beethoven Symphony 9": ["Allegro", "Molto vivace", "Adagio", "Presto"],
}


def _od_station(tmp_path: Path):
    for folder, files in ONDEMAND.items():
        d = tmp_path / "ondemand" / folder
        d.mkdir(parents=True)
        for n, name in enumerate(files, 1):
            (d / f"{n:02d} - {name}.mp3").write_bytes(b"x")
    st = _station(tmp_path, ondemand_folder=tmp_path / "ondemand")
    st.tts.last_rss_mb, st.tts.restarts = 0, 0         # (for status())
    return st


def _album(st, root, folder):
    return next(a for a in st.albums() if a["root"] == root and a["folder"] == folder)


def test_ondemand_is_never_in_the_show(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    assert len(st.ondemand_tracks) == 8 and len(st.tracks) == 9
    od = tmp_path / "ondemand"
    assert not any(t.path.is_relative_to(od) for t in st.tracks)
    assert st.search("hancock") == [] and st.search("rain") == []
    assert {a["name"] for a in st.artists()} == {"The Beatles", "Crowded House", "Nick Drake"}
    picks = {st._take_next().path for _ in range(40)}
    assert not any(p.is_relative_to(od) for p in picks)
    assert st.status()["library"]["ondemand"] == 8


def test_ondemand_folders_are_named_by_the_folder_not_a_guessed_artist(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    storm = _album(st, "ondemand", "Thunderstorms")
    assert storm["title"] == "Thunderstorms" and storm["artist"] == ""
    s1 = _album(st, "ondemand", "Old radio shows/Hancock/Series 1")
    assert s1["title"] == "Series 1" and s1["artist"] == ""          # not "Old radio shows"
    assert [t.title for t in s1["tracks"]] == ["The First Night", "The Diary"]
    assert _album(st, "music", "The Beatles/Rubber Soul")["artist"] == "The Beatles"   # music as before


def test_search_finds_folders_by_name(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    hits = st.search_albums("hancock")
    assert [(a["root"], a["title"]) for a in hits] == [("ondemand", "Series 1"), ("ondemand", "Series 2")]
    assert st.search_albums("beethoven")[0]["tracks"] == 4


def test_browse_walks_the_folders(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    top = st.browse("ondemand")
    assert [(f["name"], f["tracks"], f["folders"], f["own"]) for f in top["folders"]] == [
        ("Classical", 4, 1, False), ("Old radio shows", 3, 1, False), ("Thunderstorms", 1, 0, True)]
    assert top["album"] is None
    han = st.browse("ondemand", "Old radio shows/Hancock")
    assert [f["name"] for f in han["folders"]] == ["Series 1", "Series 2"] and han["album"] is None
    s1 = st.browse("ondemand", "Old radio shows/Hancock/Series 1")
    assert s1["folders"] == [] and s1["album"]["tracks"] == 2
    assert [f["name"] for f in st.browse("music")["folders"]] == ["Crowded House", "Nick Drake", "The Beatles"]
    for bad in (("elsewhere", ""), ("music", "../etc")):
        try:
            st.browse(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_play_all_goes_through_everything_under_a_folder_in_order(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    all_hancock = st._album_by_folder("Old radio shows/Hancock", "ondemand", deep=True)
    assert [t.title for t in all_hancock["tracks"]] == ["The First Night", "The Diary", "The Bequest"]
    assert all_hancock["title"] == "Hancock"
    st.play_album(root="ondemand", folder="Old radio shows/Hancock", deep=True)
    assert st.source["deep"] is True and st.source["root"] == "ondemand"
    assert st.status()["source"]["tracks"] == 3


def test_play_next_ondemand_waits_for_the_song_then_plays_with_no_dj(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    monkeypatch.setattr(type(st), "is_on_air", property(lambda self: True))
    reply = st.play_next(root="ondemand", folder="Thunderstorms")
    assert reply == {"title": "Thunderstorms", "artist": "", "now": False, "dj": False}
    assert st.source is None and st.status()["up_next_source"]["title"] == "Thunderstorms"
    assert st.requests() == []                          # nothing put into the show
    assert st._take_next_source() is True               # (the song ended)
    assert st.source["folder"] == "Thunderstorms" and st._switch.is_set()
    assert st.status()["up_next_source"] is None


def test_play_next_ondemand_off_air_is_simply_chosen(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    reply = st.play_next(root="ondemand", folder="Classical/Beethoven Symphony 9")
    assert reply["now"] is True and st.source["title"] == "Beethoven Symphony 9"


def test_stop_album_drops_a_queued_play_next(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    monkeypatch.setattr(type(st), "is_on_air", property(lambda self: True))
    st.play_next(root="ondemand", folder="Thunderstorms")
    assert st.stop_album() is True and st.next_source_status() is None


def test_play_next_music_still_goes_into_the_show_with_the_dj(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st._take_next()
    reply = st.play_next(_album_id(st, "Rubber Soul"))
    assert reply["dj"] is True and reply["tracks"] == 4
    assert len(st.requests()) == 4 and st.next_source_status() is None


def test_an_ondemand_album_on_a_button(tmp_path: Path) -> None:
    p = presets.validate({"kind": "album", "folder": "Old radio shows/Hancock", "title": "Hancock",
                          "root": "ondemand", "deep": True})
    assert p["root"] == "ondemand" and p["deep"] is True
    assert not presets.same(p, {**p, "deep": False})
    assert "root" not in presets.validate({"kind": "album", "folder": "A/B", "title": "B"})


def test_the_media_manager_makes_the_ondemand_folder_when_needed(tmp_path: Path) -> None:
    lib = MediaLibrary({"music": tmp_path / "music", "ondemand": tmp_path / "ondemand"}, lease=tmp_path / "lease")
    (tmp_path / "music").mkdir()
    assert lib.list("ondemand")["folders"] == []
    lib.mkdir("ondemand", "", "Thunderstorms")
    assert (tmp_path / "ondemand" / "Thunderstorms").is_dir()
    lib.done()


def test_web_api(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
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
        albums = req("/api/albums")["albums"]
        assert {a["root"] for a in albums} == {"music", "ondemand"} and len(albums) == 7
        b = req("/api/browse?root=ondemand&path=Old%20radio%20shows")
        assert b["folders"][0]["name"] == "Hancock"
        got = req("/api/album/play", {"root": "ondemand", "folder": "Old radio shows/Hancock", "deep": True})
        assert got["source"]["title"] == "Hancock" and got["source"]["deep"] is True
        nxt = req("/api/album", {"root": "ondemand", "folder": "Thunderstorms"})
        assert nxt["dj"] is False and nxt["now"] is True    # off air: chosen straight away
    finally:
        httpd.shutdown()


def test_browse_lists_an_albums_tracks(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    s1 = st.browse("ondemand", "Old radio shows/Hancock/Series 1")["album"]
    assert [(t["n"], t["title"]) for t in s1["list"]] == [(0, "The First Night"), (1, "The Diary")]
    assert s1["list"][1]["path"] == "Old radio shows/Hancock/Series 1/02 - The Diary.mp3"


def test_play_one_track_or_from_a_track(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    st.play_album(root="ondemand", folder="Classical/Beethoven Symphony 9", track=2)
    assert st.source["track"] == 2 and "one" not in st.source
    st.play_track("ondemand", "Classical/Beethoven Symphony 9/02 - Molto vivace.mp3")
    assert st.source["one"] is True and st.source["track"] == 1 and st.source["title"] == "Molto vivace"
    _on_air(st, monkeypatch)
    st.tune(None)
    st._take_next()
    st.on_air = __import__("sleepradiopi.broadcast.station", fromlist=["OnAir"]).OnAir("track", "x")
    st.play_track("music", "Nick Drake/Pink Moon/02 - Place to Be.mp3")
    assert st._jump.title == "Place to Be" and st.source is None     # the show, at once
    with __import__("pytest").raises(ValueError):
        st.play_track("music", "No/Such.mp3")


def test_one_track_plays_just_that_track_then_the_show(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    played = []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air, near_end=None: played.append(on_air.title))
    st.play_track("ondemand", "Classical/Beethoven Symphony 9/02 - Molto vivace.mp3")
    st._switch.clear()
    st._run_album(st.source)
    assert played == ["Molto vivace"] and st.source is None
    st.play_album(root="ondemand", folder="Classical/Beethoven Symphony 9", track=2)
    st._switch.clear()
    played.clear()
    st._run_album(st.source)
    assert played == ["Adagio", "Presto"]
