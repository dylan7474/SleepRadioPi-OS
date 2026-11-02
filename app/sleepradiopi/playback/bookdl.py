"""Download a followed LibriVox book to the radio, to listen to with no Wi-Fi.

The book is saved as a folder of its chapters in Audiobooks (<author> - <title>/001 - <chapter>.mp3 ...),
which is exactly what an uploaded book is, so it plays like any other: no Wi-Fi, and the whole-book bar.
It goes through MediaLibrary.receive(), the one that uploads use, so everything that makes a write to the
card safe is already there: a hidden .part file renamed in once whole, room checked first, /media made
writable only while it's needed, and a chapter that is already there with the same size is skipped -- so a
download that was cut short (Wi-Fi gone, power off) carries on where it stopped when it is started again.

One download at a time. When every chapter is in, the place you had reached while streaming is carried over
(as a whole-book position), the streamed copy is unfollowed (the folder replaces it), and the library
is rescanned at once.

The card is written only when the user presses Download (like an upload), never in the background.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.media import KEEP_FREE, MediaError
from sleepradiopi.playback.audiobooks import SIDECAR
from sleepradiopi.playback import librivox as lv_mod
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

KIND = "audiobooks"
KBPS = 64                     # LibriVox's files are 64 kbps mp3: the size is worked out from the lengths
TRIES = 3                     # a chapter cut off part-way is fetched again this many times
RESUMES = 30                  # ...and a connection that ends early is picked up again where it stopped this many times


class DownloadError(ValueError):
    """Couldn't start the download: the text is for the page."""


def _open(url: str, start: int = 0, timeout: float = 30):
    """The file at url (from byte `start` on: a Range request, answered 206)."""
    headers = {"User-Agent": radio_mod.USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def resumable_read(open_url, url: str, resp, length: int, cancel: threading.Event, on_bytes, name: str = ""):
    """A read(n) for MediaLibrary.receive() that carries on where the connection stopped. archive.org (and
    others) hang up on the radio part-way through a file: when the data ends before `length`, it asks again from
    the byte reached (a Range request, answered 206) instead of starting over. Returns (read, close): close()
    shuts the extra connections it opened. A server that ignores the Range ends the data early as before."""
    got, resumes, cur = 0, 0, [resp]

    def read(n: int) -> bytes:
        nonlocal got, resumes
        if cancel.is_set():
            raise MediaError("cancelled")
        data = cur[-1].read(n)
        while not data and got < length and resumes < RESUMES:
            resumes += 1
            log.info("download: %s: the connection ended at %d of %d bytes; carrying on (%d)", name, got, length, resumes)
            if cancel.is_set():
                raise MediaError("cancelled")
            nxt = open_url(url, got)
            if getattr(nxt, "status", 206) != 206:        # (it ignored the Range: can't carry on)
                nxt.close()
                break
            cur.append(nxt)
            data = nxt.read(n)
        got += len(data)
        on_bytes(len(data))
        return data

    def close() -> None:
        for extra in cur[1:]:
            try:
                extra.close()
            except OSError:
                pass

    return read, close


def _clean(text: str) -> str:
    """A name safe as a file or folder name (no slashes or other marks files can't have)."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(text))
    return " ".join(text.split()).strip(" .")


def folder_name(feed: dict) -> str:
    title = _clean(feed.get("title") or "LibriVox book")[:80] or "LibriVox book"
    author = _clean(feed.get("author") or "")[:60]
    return f"{author} - {title}" if author else title


def chapter_file(number: int, title: str) -> str:
    return f"{number:03d} - {_clean(title)[:70] or 'Chapter'}.mp3"


def estimate_bytes(episodes: list[dict]) -> int:
    return int(sum(e.get("duration_ms") or 0 for e in episodes) / 1000 * KBPS * 125)


class BookDownloader:
    def __init__(self, podcasts, media, positions, open_url=_open, retry_wait_s: float = 5.0) -> None:
        self.podcasts = podcasts          # playback/podcasts.Podcasts (where the followed book and its chapters are)
        self.media = media                # media.MediaLibrary
        self.positions = positions        # audiobooks.Positions
        self.open_url = open_url
        self.retry_wait_s = retry_wait_s
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"state": "idle"}

    # --- asking ---------------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def start(self, sid: str) -> dict:
        show = self.podcasts.show(sid)
        if show is None or not lv_mod.is_book(show["feed_url"]):
            raise DownloadError("only a LibriVox book you follow can be downloaded")
        feed = self.podcasts.feed(sid)
        if not feed or not feed.get("episodes"):
            raise DownloadError("this book's chapters aren't known yet: open it once while the radio is online")
        chapters = sorted(feed["episodes"], key=lambda e: e["pub"])
        need = estimate_bytes(chapters)
        free = self.media.usage()["free"]
        if need + KEEP_FREE > free:
            raise DownloadError(f"not enough room: it needs about {need >> 20} MB and the radio has {max(0, free - KEEP_FREE) >> 20} MB to spare")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise DownloadError(f"already downloading “{self.state.get('title', 'a book')}”: one at a time")
            self._cancel.clear()
            self.state = {"state": "running", "id": sid, "title": feed["title"], "folder": folder_name(feed),
                          "chapter": 0, "chapters": len(chapters), "bytes": 0, "estimate": need, "error": "",
                          "started": time.time()}
            self._thread = threading.Thread(target=self._run, args=(sid, feed, chapters), name="book-download", daemon=True)
            self._thread.start()
            return dict(self.state)

    def cancel(self) -> dict:
        self._cancel.set()
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    # --- the download ---------------------------------------------------------------------

    def _chapter(self, folder: str, ch: dict, number: int) -> None:
        name = chapter_file(number, ch["title"])
        last: Exception | None = None
        for attempt in range(1, TRIES + 1):
            if self._cancel.is_set():
                raise MediaError("cancelled")
            try:
                with self.open_url(ch["url"]) as resp:
                    length = int(resp.headers.get("Content-Length") or 0)
                    if length <= 0:
                        raise MediaError(f"{name}: the server didn't say how big it is")
                    read, close = resumable_read(self.open_url, ch["url"], resp, length, self._cancel,
                                                 lambda n: self._set(bytes=self.state["bytes"] + n), name)
                    try:
                        res = self.media.receive(KIND, folder, name, length, read)
                    finally:
                        close()
                    if res["status"] == "same":                     # (already there: count its bytes as done)
                        self._set(bytes=self.state["bytes"] + length)
                    return
            except MediaError as e:
                if "cancelled" in str(e) or "room" in str(e) or "writable" in str(e):
                    raise
                last = e
            except OSError as e:                                    # the connection dropped or timed out
                last = e
            log.info("librivox download: %s try %d failed: %s", name, attempt, last)
            if attempt < TRIES:
                time.sleep(self.retry_wait_s)
        raise MediaError(f"{name}: couldn't fetch it ({last})")

    def _run(self, sid: str, feed: dict, chapters: list[dict]) -> None:
        folder = self.state["folder"]
        try:
            for i, ch in enumerate(chapters, 1):
                self._set(chapter=i)
                self._chapter(folder, ch, i)
            self._label(folder, feed)
            self._finish(sid, feed, chapters, folder)
            self._set(state="done", chapter=len(chapters))
            log.info("librivox: downloaded %s (%d chapters)", folder, len(chapters))
        except MediaError as e:
            cancelled = "cancelled" in str(e)
            self._set(state="cancelled" if cancelled else "error", error="" if cancelled else str(e))
            log.info("librivox download of %s stopped: %s", folder, e)
        except Exception as e:                                      # (never leave the page waiting on a dead job)
            log.exception("librivox download failed")
            self._set(state="error", error=f"{type(e).__name__}: {e}")
        finally:
            self.media.done()          # (the card read-only again, the library rescanned: the new book shows)

    def _label(self, folder: str, feed: dict) -> None:
        """The folder says what the book is (the files' own tags name the LibriVox series, not the book)."""
        root = self.media.roots[KIND]
        self.media._open(root)                                       # (keep the card writable for this too)
        write_atomic(root / folder / SIDECAR, json.dumps({"title": feed["title"], "author": feed.get("author", ""),
                                                          "source": "librivox", "id": feed.get("id", "")}))

    def _finish(self, sid: str, feed: dict, chapters: list[dict], folder: str) -> None:
        """Carry the streamed place over (whole-book ms), then drop the streamed copy."""
        try:
            eps = {e["guid"]: e for e in self.podcasts.episodes(sid)}
            cur = self.podcasts.pick(sid)
            if cur is not None and cur["pos_ms"] > 0:
                before = sum(c.get("duration_ms") or 0 for c in chapters if c["pub"] < cur["pub"])
                self.positions.set(folder, int(before + cur["pos_ms"]))
            elif cur is not None and any(e["done"] for e in eps.values()):
                before = sum(c.get("duration_ms") or 0 for c in chapters if c["pub"] < cur["pub"])
                if before:
                    self.positions.set(folder, int(before))
        except Exception:
            log.exception("librivox download: couldn't carry the place over")
        self.podcasts.unsubscribe(sid)
