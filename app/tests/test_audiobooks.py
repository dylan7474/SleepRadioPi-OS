import io
import json
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.config.settings import Settings
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.media import MediaError, MediaLibrary
from sleepradiopi.playback.audiobooks import BookLibrary, Positions
from sleepradiopi.web.server import make_handler

from test_offline import FakeTts, NullOutput

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def _mp3(path: Path, seconds: float, title: str = "", album: str = "", artist: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in (("title", title), ("album", album), ("artist", artist)) if v for x in ("-metadata", f"{k}={v}")]
    subprocess.run([*FF, "-f", "lavfi", "-i", f"sine=d={seconds}", *meta, "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
                   check=True)


def _m4b(path: Path, chapters: list[tuple[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(s for _, s in chapters)
    raw = path.with_suffix(".m4a")
    subprocess.run([*FF, "-f", "lavfi", "-i", f"sine=d={total}", "-c:a", "aac", "-b:a", "32k", str(raw)], check=True)
    meta, t = [";FFMETADATA1", "title=The Hobbit", "artist=J. R. R. Tolkien"], 0
    for name, secs in chapters:
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t * 1000)}", f"END={int((t + secs) * 1000)}", f"title={name}"]
        t += secs
    (path.parent / "meta.txt").write_text("\n".join(meta) + "\n")
    subprocess.run([*FF, "-i", str(raw), "-i", str(path.parent / "meta.txt"), "-map_metadata", "1", "-map_chapters", "1",
                    "-c", "copy", "-f", "mp4", str(path)], check=True)
    raw.unlink()
    (path.parent / "meta.txt").unlink()


@pytest.fixture(scope="module")
def books_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("audiobooks")
    for i in (1, 2, 10):                                     # natural order: 1, 2, 10
        _mp3(root / "Agatha Christie" / "Poirot" / f"{i:d} - Part {i}.mp3", 2, album="Poirot Stories",
             artist="Agatha Christie")
    _m4b(root / "The Hobbit.m4b", [("An Unexpected Party", 3), ("Roast Mutton", 2)])
    _mp3(root / "Short Story.mp3", 1.5)
    return root


def test_the_library_finds_books_and_chapters(books_dir: Path, tmp_path: Path) -> None:
    lib = BookLibrary(books_dir, tmp_path / "cache.json")
    books = {b.key: b for b in lib.scan()}
    assert set(books) == {"Agatha Christie/Poirot", "The Hobbit.m4b", "Short Story.mp3"}
    poirot = books["Agatha Christie/Poirot"]
    assert poirot.title == "Poirot Stories" and poirot.author == "Agatha Christie"
    assert [c.title for c in poirot.chapters] == ["Part 1", "Part 2", "Part 10"]
    hobbit = books["The Hobbit.m4b"]
    assert hobbit.title == "The Hobbit" and hobbit.author == "J. R. R. Tolkien"
    assert [(c.title, c.start_ms, c.end_ms) for c in hobbit.chapters] == \
        [("An Unexpected Party", 0, 3000), ("Roast Mutton", 3000, 5000)]
    assert hobbit.total_ms == 5000 and hobbit.at(3500) == (1, 500) and hobbit.at(99_999)[0] == 1
    assert books["Short Story.mp3"].chapters[0].length_ms > 1000
    # the second scan comes from the cache (no ffmpeg for the m4b)
    lib2 = BookLibrary(books_dir, tmp_path / "cache.json")
    import sleepradiopi.playback.audiobooks as ab
    calls = []
    real = ab._probe
    ab._probe = lambda p: calls.append(p) or real(p)
    try:
        assert len(lib2.scan()) == 3 and calls == []
    finally:
        ab._probe = real


def test_positions_are_kept(tmp_path: Path) -> None:
    p = Positions(tmp_path / "pos.json")
    p.set("Book", 123_456)
    assert Positions(tmp_path / "pos.json").get("Book") == 123_456 and p.get("Other") == 0


# --- the player --------------------------------------------------------------------------

class Out:
    def __init__(self, then=None):
        self.blocks, self.then = 0, then

    def start(self): ...
    def stop(self): ...

    def write(self, block):
        self.blocks += 1
        if self.then:
            self.then(self.blocks)


def _station(tmp_path: Path, books_dir: Path) -> station_mod.Station:
    music = tmp_path / "music" / "A" / "B"
    music.mkdir(parents=True)
    (music / "01 - x.mp3").write_bytes(b"x")
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "none", hooks_file="",
               scan_cache=tmp_path / "scans.json", tag_cache=None, audiobooks_folder=books_dir,
               book_cache=tmp_path / "books.json", book_positions=tmp_path / "pos.json")
    st = station_mod.Station(cfg, FakeTts(), NullOutput())
    for _ in range(100):
        if not st.books_scanning:
            break
        threading.Event().wait(0.05)
    st.tts = None
    st._listeners = 1
    return st


@pytest.fixture
def fake_decode(monkeypatch):
    """pcm.decode that 'plays' [end - start] ms of silence, in 100 ms blocks; records its calls."""
    calls = []

    def decode(path, start_ms=0, end_ms=0):
        calls.append((Path(path).name, start_ms, end_ms))
        n = max(0, (end_ms - start_ms) // 100)
        return (np.zeros((4410, 2), np.int16) for _ in range(n))
    monkeypatch.setattr(pcm, "decode", decode)
    return calls


def test_a_book_plays_on_from_its_place_and_pauses_the_radio_at_the_end(tmp_path, books_dir, fake_decode) -> None:
    st = _station(tmp_path, books_dir)
    ended = []
    st.on_book_end = lambda: ended.append(1) or setattr(st, "_listeners", 0)
    st.book_positions.set("The Hobbit.m4b", 3500)             # left in chapter 2
    st.tune({"kind": "book", "key": "The Hobbit.m4b"})
    st._switch.clear()
    st.output = Out()
    st._run_book(st.source)
    assert fake_decode == [("The Hobbit.m4b", 3500, 5000)]    # from the place, to the chapter's end
    assert ended == [1] and st.book_positions.get("The Hobbit.m4b") == 5000
    st._listeners = 1
    st._switch.clear()
    st._run_book(st.source)                                   # finished: from the start next time
    assert fake_decode[1] == ("The Hobbit.m4b", 0, 3000)


def test_pause_keeps_the_place_and_steps_back_a_minute_after_the_sleep_timer(tmp_path, books_dir, fake_decode,
                                                                           monkeypatch) -> None:
    monkeypatch.setattr(station_mod, "BOOK_BACK_SLEEP_MS", 1000)   # (the test book is only seconds long)
    monkeypatch.setattr(station_mod, "BOOK_BACK_PAUSE_MS", 200)
    st = _station(tmp_path, books_dir)
    slept = [True]
    st.paused_by_sleep = lambda: slept[0]
    st.tune({"kind": "book", "key": "The Hobbit.m4b"})
    st._switch.clear()

    def then(n):
        if n == 20:                                           # 2 s in: the sleep timer pauses it
            st._listeners = 0
            threading.Timer(0.3, lambda: st._stop.set()).start()   # ...and the show ends
    st.output = Out(then)
    st._run_book(st.source)
    assert st.book_positions.get("The Hobbit.m4b") == 1000    # 2 s - the step back
    assert st.book_now is None and st.status()["source"]["pos_ms"] == 1000   # the page shows the kept place
    slept[0] = False
    st._listeners, st._stop = 1, threading.Event()
    st.output = Out(lambda n: n == 5 and (setattr(st, "_listeners", 0),
                                           threading.Timer(0.3, st._stop.set).start()))
    st._switch.clear()
    st._run_book(st.source)                                   # an ordinary pause, 0.5 s on from 1 s
    assert st.book_positions.get("The Hobbit.m4b") == 1300    # 1.5 s - 0.2 s


def test_seek_moves_the_book_on_air_and_while_paused(tmp_path, books_dir, fake_decode) -> None:
    st = _station(tmp_path, books_dir)
    with pytest.raises(ValueError):
        st.book_seek(delta_ms=60_000)                         # no book on
    st.tune({"kind": "book", "key": "The Hobbit.m4b"})
    assert st.book_seek(delta_ms=4000)["pos_ms"] == 4000      # not playing: saved for later
    assert st.book_positions.get("The Hobbit.m4b") == 4000
    assert st.book_seek(delta_ms=-60_000)["pos_ms"] == 0 and st.book_seek(to_ms=99_999)["pos_ms"] == 4000
    st._switch.clear()

    def then(n):
        if n == 3:
            st.book_seek(to_ms=500)                           # on air: the player jumps
        if n == 8:
            st._stop.set()
    st.book_positions.set("The Hobbit.m4b", 0)
    st.output = Out(then)
    st._run_book(st.source)
    assert fake_decode[0][1] == 0 and fake_decode[1] == ("The Hobbit.m4b", 500, 3000)


def test_books_on_the_buttons_and_the_api(tmp_path, books_dir, fake_decode) -> None:
    st = _station(tmp_path, books_dir)
    p = presets_mod.Presets(st, None, None, [{"kind": "book", "key": "The Hobbit.m4b", "title": "The Hobbit"}])
    assert p.status()["buttons"][0]["label"] == "The Hobbit"
    p.press(0)
    assert st.source["kind"] == "book" and p.status()["buttons"][0]["playing"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None))
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
        code, d = call("/api/books")
        assert code == 200 and {b["title"] for b in d["books"]} == {"Poirot Stories", "The Hobbit", "Short Story"}
        code, d = call("/api/books/play", {"key": "Agatha Christie/Poirot"})
        assert code == 200 and d["source"]["title"] == "Poirot Stories" and d["source"]["chapters"] == 3
        code, d = call("/api/books/seek", {"delta_ms": 1500})
        assert code == 200 and d["pos_ms"] == 1500 and d["chapter"] == 1
        code, d = call("/api/books/play", {"key": "../../etc/passwd"})
        assert code == 400
    finally:
        httpd.shutdown()


def test_books_can_be_uploaded(tmp_path: Path) -> None:
    roots = {k: tmp_path / k for k in ("music", "jingles", "audiobooks")}
    for r in roots.values():
        r.mkdir()
    lib = MediaLibrary(roots, lease=tmp_path / "lease")
    assert lib.receive("audiobooks", "", "Tolkien/The Hobbit.m4b", 4, io.BytesIO(b"book").read)["status"] == "added"
    with pytest.raises(MediaError):
        lib.receive("music", "", "The Hobbit.m4b", 4, io.BytesIO(b"book").read)
