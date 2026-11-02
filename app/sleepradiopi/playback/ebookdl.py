"""Download a Project Gutenberg book into the Ebooks folder, to be read aloud.

Like playback/bookdl.py (LibriVox) it goes through MediaLibrary.receive(), the one uploads use: a hidden .part file
renamed in once whole, room checked first, /media writable only while it's needed. And like it, a connection that
ends early carries on from where it stopped (bookdl.resumable_read). The book is one file, pg<number>.txt (or .epub
where there is no text), named as Gutenberg names it, so the same book twice is the same file.

start() first asks the server how big the file is (opening it and reading nothing). A book bigger than LARGE_BYTES
(a collected works, say) is not fetched until the caller says again with confirm=True: a normal novel is under 2 MB.
"""

from __future__ import annotations

import logging
import threading
import time

from sleepradiopi.media import MediaError
from sleepradiopi.playback import bookdl
from sleepradiopi.playback.gutenberg import FILES, GutenbergError, book_id

log = logging.getLogger(__name__)

KIND = "ebooks"
LARGE_BYTES = 3 * 1024 * 1024
TRIES = 3


class EbookDownloader:
    def __init__(self, media, open_url=bookdl._open, retry_wait_s: float = 5.0) -> None:
        self.media = media
        self.open_url = open_url
        self.retry_wait_s = retry_wait_s
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def _probe(self, bid: str) -> tuple[str, str, int]:
        """(url, extension, bytes) of the book's best file: the first of FILES the server has."""
        for ext, template in FILES:
            url = template.format(id=bid)
            try:
                with self.open_url(url) as resp:
                    length = int(resp.headers.get("Content-Length") or 0)
            except OSError:
                continue
            if length > 0:
                return url, ext, length
        raise GutenbergError("Project Gutenberg has no readable file for that book")

    def start(self, raw_id, title: str = "", confirm: bool = False) -> dict:
        bid = book_id(raw_id)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise GutenbergError(f"already downloading “{self.state.get('title', 'a book')}”: one at a time")
        url, ext, length = self._probe(bid)
        title = " ".join(str(title or "").split())[:200] or f"Book {bid}"
        if length > LARGE_BYTES and not confirm:
            return {"state": "confirm", "id": bid, "title": title, "bytes": length}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise GutenbergError(f"already downloading “{self.state.get('title', 'a book')}”: one at a time")
            self._cancel.clear()
            self.state = {"state": "running", "kind": "ebook", "id": bid, "title": title, "chapter": 1, "chapters": 1,
                          "bytes": 0, "estimate": length, "error": "", "started": time.time()}
            self._thread = threading.Thread(target=self._run, args=(url, f"pg{bid}{ext}", length),
                                            name="ebook-download", daemon=True)
            self._thread.start()
            return dict(self.state)

    def cancel(self) -> dict:
        self._cancel.set()
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _run(self, url: str, name: str, length: int) -> None:
        last: Exception | None = None
        try:
            for attempt in range(1, TRIES + 1):
                if self._cancel.is_set():
                    raise MediaError("cancelled")
                try:
                    with self.open_url(url) as resp:
                        got = int(resp.headers.get("Content-Length") or 0)
                        if got <= 0:
                            raise MediaError(f"{name}: the server didn't say how big it is")
                        read, close = bookdl.resumable_read(self.open_url, url, resp, got, self._cancel,
                                                            lambda n: self._set(bytes=self.state["bytes"] + n), name)
                        try:
                            self.media.receive(KIND, "", name, got, read)
                        finally:
                            close()
                    self.media.done()          # (the card read-only again, the library rescanned: the new book shows)
                    self._set(state="done", bytes=length)       # ...and only then "done": the page refreshes the folder on it
                    log.info("ebook download: %s done", name)
                    return
                except MediaError as e:
                    if any(w in str(e) for w in ("cancelled", "room", "writable")):
                        raise
                    last = e
                except OSError as e:
                    last = e
                self._set(bytes=0)
                log.info("ebook download: %s try %d failed: %s", name, attempt, last)
                if attempt < TRIES:
                    time.sleep(self.retry_wait_s)
            raise MediaError(f"{name}: couldn't fetch it ({last})")
        except MediaError as e:
            msg = str(e)
            self.media.done()                                        # (read-only again even though it stopped)
            self._set(state="cancelled" if "cancelled" in msg else "error", error="" if "cancelled" in msg else msg)
            log.info("ebook download of %s stopped: %s", name, msg)
        except Exception as e:                                       # (never a silent dead thread)
            log.exception("ebook download failed")
            try:
                self.media.done()
            except Exception:
                log.exception("library rescan failed")
            self._set(state="error", error=str(e))
