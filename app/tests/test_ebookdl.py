import time
import urllib.error

import pytest

from sleepradiopi.media import MediaLibrary
from sleepradiopi.playback import ebookdl
from sleepradiopi.playback.gutenberg import GutenbergError

TEXT = ("It is a truth universally acknowledged. " * 200).encode()


class Resp:
    def __init__(self, data, status=200, cut=None):
        self.data, self.pos, self.status, self.cut = data, 0, status, cut
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        n = len(self.data) - self.pos if n is None or n < 0 else n
        if self.cut is not None:
            n = min(n, self.cut - self.pos)
            if n <= 0:
                return b""
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Server:
    """A fake gutenberg.org: ids in `have` have a text file, ids in `epub_only` have only an epub."""

    def __init__(self, data=TEXT, epub_only=(), cut=None, missing=()):
        self.data, self.epub_only, self.cut, self.missing = data, set(epub_only), cut, set(missing)
        self.asked = []

    def __call__(self, url, start=0):
        self.asked.append((url, start))
        bid = url.split("/ebooks/")[1].split(".")[0]
        if bid in self.missing or (url.endswith(".txt.utf-8") and bid in self.epub_only):
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        data = self.data[start:] if start else self.data
        return Resp(data, status=206 if start else 200, cut=self.cut)


RESCANS = []


def setup(tmp_path, server=None):
    RESCANS.clear()
    media = MediaLibrary({"ebooks": tmp_path / "ebooks"}, on_changed=RESCANS.append, lease=tmp_path / "lease")
    server = server or Server()
    return ebookdl.EbookDownloader(media, open_url=server, retry_wait_s=0), server, tmp_path / "ebooks"


def media_leased(dl):
    return dl.media.lease.exists()


def run(dl, *a, **kw):
    r = dl.start(*a, **kw)
    dl.wait(20)
    return r, dl.status()


def test_a_book_is_downloaded_into_the_ebooks_folder_as_gutenberg_names_it(tmp_path):
    dl, server, folder = setup(tmp_path)
    first, st = run(dl, "1342", "Pride and Prejudice")
    assert first["state"] == "running" and st["state"] == "done" and st["bytes"] == len(TEXT)
    assert (folder / "pg1342.txt").read_bytes() == TEXT
    assert RESCANS == [{"ebooks"}]            # the radio's library was rescanned BEFORE "done": the folder shows the book


def test_a_book_with_no_text_comes_as_an_epub(tmp_path):
    dl, server, folder = setup(tmp_path, Server(epub_only=["99"]))
    run(dl, "99", "Some Book")
    assert (folder / "pg99.epub").read_bytes() == TEXT


def test_a_book_gutenberg_does_not_have_says_so(tmp_path):
    dl, *_ = setup(tmp_path, Server(missing=["7"]))
    with pytest.raises(GutenbergError, match="no readable file"):
        dl.start("7", "Nothing")


def test_a_big_book_waits_to_be_confirmed(tmp_path):
    dl, server, folder = setup(tmp_path, Server(data=b"x" * (ebookdl.LARGE_BYTES + 1)))
    r = dl.start("31100", "The Complete Works of Jane Austen")
    assert r["state"] == "confirm" and r["bytes"] == ebookdl.LARGE_BYTES + 1 and r["title"].startswith("The Complete")
    assert dl.status()["state"] == "idle" and not list(folder.glob("*"))      # nothing fetched, nothing written
    r, st = run(dl, "31100", "The Complete Works of Jane Austen", confirm=True)
    assert st["state"] == "done" and (folder / "pg31100.txt").stat().st_size == ebookdl.LARGE_BYTES + 1


def test_a_connection_that_ends_early_carries_on_where_it_stopped(tmp_path):
    dl, server, folder = setup(tmp_path, Server(cut=1500))
    _, st = run(dl, "1342", "Pride and Prejudice")
    assert st["state"] == "done", st
    assert (folder / "pg1342.txt").read_bytes() == TEXT
    assert any(start > 0 for _, start in server.asked)


def test_only_one_download_at_a_time_and_the_number_is_checked(tmp_path):
    dl, *_ = setup(tmp_path)
    with pytest.raises(GutenbergError):
        dl.start("../etc", "x")
    dl._thread = type("T", (), {"is_alive": lambda self: True})()
    dl.state = {"state": "running", "title": "Another"}
    with pytest.raises(GutenbergError, match="one at a time"):
        dl.start("1342", "Pride and Prejudice")


def test_not_enough_room_is_an_error_not_a_hang(tmp_path, monkeypatch):
    dl, server, folder = setup(tmp_path)
    monkeypatch.setattr(dl.media, "usage", lambda: {"free": 0})
    _, st = run(dl, "1342", "Pride and Prejudice")
    assert st["state"] == "error" and "room" in st["error"]
    assert not media_leased(dl)                # (read-only again, even though it stopped)
