import json
import threading
from pathlib import Path

import pytest

from sleepradiopi.media import MediaLibrary
from sleepradiopi.playback import bookdl, podcasts as pod_mod
from sleepradiopi.playback.audiobooks import Positions

BOOK = json.loads((Path(__file__).parent / "librivox_book.json").read_text())      # real, trimmed to 3 chapters
FOLDER = "Jane Austen - Pride and Prejudice"


class Feed:
    def __call__(self, url, limit, headers=None):
        return json.dumps({"books": [BOOK]}).encode()


class Resp:
    def __init__(self, data: bytes, headers=None, status=200, cut=None):
        self.data, self.pos = data, 0
        self.headers = {"Content-Length": str(len(data))} if headers is None else headers
        self.status = status
        self.cut = cut                  # the connection ends early, after this many bytes (a server that hangs up)

    def close(self):
        pass

    def read(self, n=-1):
        n = len(self.data) - self.pos if n is None or n < 0 else n
        if self.cut is not None:
            n = min(n, self.cut - self.pos)
            if n <= 0:
                return b""
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Server:
    """A fake archive.org: every chapter is a few KB of its own bytes; some tries can be made to fail."""

    def __init__(self, fail_first=0, die_on=None, cut_at=None, ranges=True):
        self.opened, self.fail_first, self.die_on = [], fail_first, die_on
        self.cut_at, self.ranges, self.starts = cut_at, ranges, []

    def __call__(self, url, start=0):
        self.opened.append(url)
        self.starts.append(start)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("connection reset")
        if self.die_on and self.die_on in url:
            raise OSError("down")
        data = (url.rsplit("/", 1)[-1] * 400).encode()
        if start and self.ranges:
            data = data[start:]
            return Resp(data, status=206, cut=self.cut_at)
        return Resp(data, cut=self.cut_at)


def setup(tmp_path, server=None, **kw):
    pos = Positions(tmp_path / "pos.json")
    pods = pod_mod.Podcasts([], tmp_path / "pods", pos, book_fetch=Feed())
    sid = pods.subscribe("librivox:253")["id"]
    changed = []
    media = MediaLibrary({"audiobooks": tmp_path / "audiobooks"}, on_changed=changed.append, lease=tmp_path / "lease")
    dl = bookdl.BookDownloader(pods, media, pos, open_url=server or Server(), retry_wait_s=0, **kw)
    return dl, pods, sid, pos, media, changed


def run(dl, sid):
    dl.start(sid)
    dl.wait(20)
    return dl.status()


def test_names():
    assert bookdl.folder_name({"title": "Pride and Prejudice", "author": "Jane Austen"}) == FOLDER
    assert bookdl.folder_name({"title": "A/B: C?", "author": ""}).count("/") == 0
    assert bookdl.chapter_file(2, "Chapters 4-5") == "002 - Chapters 4-5.mp3"


def test_downloads_every_chapter_as_a_folder_book_and_unfollows(tmp_path):
    dl, pods, sid, pos, media, changed = setup(tmp_path)
    st = run(dl, sid)
    assert st["state"] == "done" and st["chapters"] == 3 and st["bytes"] > 0
    files = sorted(p.name for p in (tmp_path / "audiobooks" / FOLDER).glob("*.mp3"))
    assert json.loads((tmp_path / "audiobooks" / FOLDER / ".book.json").read_text())["title"] == "Pride and Prejudice"
    assert len(files) == 3
    assert files[0].startswith("001 - ") and files[2].startswith("003 - ")
    assert pods.show(sid) is None                          # the streamed copy is replaced by the folder
    assert changed == [{"audiobooks"}]                     # the library was rescanned at once
    assert not (tmp_path / "lease").exists()               # and the card let go read-only
    assert not list((tmp_path / "audiobooks" / ".incoming").glob("*.part"))


def test_the_streamed_place_is_carried_into_the_whole_book(tmp_path):
    dl, pods, sid, pos, media, _ = setup(tmp_path)
    eps = pods.episodes(sid)[::-1]                          # chapter 1 first
    pos.set(pod_mod.episode_key(sid, eps[0]["guid"]), 1, done=True)
    pos.set(pod_mod.episode_key(sid, eps[1]["guid"]), 5000)
    run(dl, sid)
    assert pos.get(FOLDER) == eps[0]["duration_ms"] + 5000


def test_a_cut_download_carries_on_where_it_stopped(tmp_path):
    server = Server(die_on=BOOK["sections"][2]["listen_url"].rsplit("/", 1)[-1])
    dl, pods, sid, pos, media, _ = setup(tmp_path, server)
    st = run(dl, sid)
    assert st["state"] == "error" and "couldn't fetch" in st["error"]
    assert pods.show(sid) is not None                       # still followed: nothing is lost
    done = sorted(p.name for p in (tmp_path / "audiobooks" / FOLDER).glob("*.mp3"))
    assert len(done) == 2
    server.die_on = None
    server.opened.clear()
    st = run(dl, sid)
    assert st["state"] == "done"
    assert len(list((tmp_path / "audiobooks" / FOLDER).glob("*.mp3"))) == 3
    assert pods.show(sid) is None


def test_a_dropped_connection_is_tried_again(tmp_path):
    dl, pods, sid, *_ = setup(tmp_path, Server(fail_first=2))
    assert run(dl, sid)["state"] == "done"


def test_not_enough_room_is_said_before_anything_is_written(tmp_path):
    dl, pods, sid, pos, media, _ = setup(tmp_path)
    media.usage = lambda: {"total": 10, "used": 9, "free": 1}
    with pytest.raises(bookdl.DownloadError, match="not enough room"):
        dl.start(sid)
    assert not (tmp_path / "audiobooks").exists()


def test_only_followed_librivox_books_and_one_at_a_time(tmp_path):
    dl, pods, sid, *_ = setup(tmp_path)
    with pytest.raises(bookdl.DownloadError, match="LibriVox"):
        dl.start("nope")
    gate = threading.Event()
    slow = Server()
    real = slow.__call__

    def waiting(url):
        gate.wait(10)
        return real(url)
    dl.open_url = waiting
    dl.start(sid)
    with pytest.raises(bookdl.DownloadError, match="one at a time"):
        dl.start(sid)
    dl.cancel()
    gate.set()
    dl.wait(20)
    assert dl.status()["state"] == "cancelled"
    assert pods.show(sid) is not None


def test_a_server_that_does_not_say_the_size_is_an_error_not_a_hang(tmp_path):
    class NoLength(Server):
        def __call__(self, url):
            return Resp(b"abc", headers={})
    dl, pods, sid, *_ = setup(tmp_path, NoLength())
    st = run(dl, sid)
    assert st["state"] == "error" and "how big" in st["error"]


def test_the_book_keeps_its_own_title_not_the_series_tag(tmp_path):
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    from sleepradiopi.playback.audiobooks import BookLibrary

    class Real(Server):                                    # (real, tiny mp3s whose tags name the SERIES, as LibriVox's do)
        def __call__(self, url):
            out = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2", "-metadata", "title=LibriVox Weekly Poetry",
                                  "-metadata", "artist=Various", "-f", "mp3", "-"], capture_output=True, check=True).stdout
            return Resp(out)
    dl, pods, sid, *_ = setup(tmp_path, Real())
    assert run(dl, sid)["state"] == "done"
    books = BookLibrary(tmp_path / "audiobooks").scan()
    assert [(b.title, b.author, len(b.chapters)) for b in books] == [("Pride and Prejudice", "Jane Austen", 3)]


def test_a_connection_that_ends_early_is_picked_up_where_it_stopped(tmp_path):
    """archive.org hung up 12-14 MB into a 19 MB part, every time: it carries on with a Range request."""
    server = Server(cut_at=1000)
    dl, pods, sid, *_ = setup(tmp_path, server)
    st = run(dl, sid)
    assert st["state"] == "done", st
    assert any(start > 0 for start in server.starts)        # it asked to carry on, not to start again
    assert server.starts.count(0) == 3                        # (one first request per chapter: no restarts)
    files = sorted((tmp_path / "audiobooks" / FOLDER).glob("*.mp3"))
    assert len(files) == 3
    for ch, f in zip(sorted(BOOK["sections"], key=lambda s: int(s["section_number"])), files):
        assert f.read_bytes() == (ch["listen_url"].rsplit("/", 1)[-1] * 400).encode()      # whole and in order


def test_a_server_that_ignores_the_range_falls_back_to_trying_the_chapter_again(tmp_path):
    dl, pods, sid, *_ = setup(tmp_path, Server(cut_at=1000, ranges=False))
    st = run(dl, sid)
    assert st["state"] == "error" and "couldn't fetch" in st["error"]
