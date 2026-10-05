"""Who a number is: DMR IDs -> callsigns (see room.py).

Many rooms are bridged to DMR, and a talker who comes in that way arrives
under their DMR ID (seven figures, 2345153) instead of a callsign. The
register of who has which ID is radioid.net's; Pi-Star keeps a copy as a
plain list (one a line: id, callsign, first name, tab between), which is
what's fetched here, once a week, in the background.

It's a third of a million lines, too many to hold in memory on a small Pi,
so it's kept as a file of fixed-size records in ID order (the ID, then the
callsign) and looked up by halving, a few reads a talker.
"""

from __future__ import annotations

import http.client
import logging
import os
import struct
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE = "https://www.pistar.uk/downloads/DMRIds.dat"
STALE_S = 7 * 24 * 3600
RETRY_S = 3600
TRIES = 6
RECORD = struct.Struct(">I10s")          # (most significant byte first: records sort as their IDs do)


def _get_lines(url: str, timeout: float = 30.0) -> Iterable[bytes]:
    """The list, a line at a time. The server often stops part-way through a download this size
    without saying so: it's then asked for the rest, from where it stopped."""
    got, size, rest = 0, None, b""
    for _ in range(TRIES):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux) SleepRadio", **({"Range": f"bytes={got}-"} if got else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if got and r.status != 206:                       # (it won't give a part: nothing for it but the start again)
                raise ValueError("the download stopped part-way")
            if size is None:
                length = r.headers.get("Content-Length")
                size = int(length) if length and length.isdigit() else 0
            try:
                while block := r.read(65536):
                    got += len(block)
                    *lines, rest = (rest + block).split(b"\n")
                    yield from lines
            except (OSError, http.client.HTTPException):      # (cut off, and said so: the same)
                pass
        if got >= size:
            if rest:
                yield rest
            return
    raise ValueError(f"the download stopped part-way ({got} of {size} bytes)")


class Ids:
    def __init__(self, path: Path, fetch: Callable[[str], Iterable[bytes]] = _get_lines, clock: Callable[[], float] = time.time) -> None:
        self.path, self._fetch, self._clock = path, fetch, clock
        self._lock = threading.Lock()
        self._refreshing = False
        self._tried = -1e9
        self._known: dict[int, str | None] = {}      # the few asked for lately

    def refresh(self) -> int:
        """Fetch the list and keep it. Returns how many IDs it has (0: it couldn't be had)."""
        tmp = self.path.with_suffix(".new")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            n, last, in_order = 0, -1, True
            with open(tmp, "wb") as f:
                for line in self._fetch(SOURCE):
                    p = line.split(b"\t")
                    if len(p) < 2 or not p[0].isdigit() or len(p[0]) > 9 or not p[1].strip():
                        continue
                    i = int(p[0])
                    in_order, last = in_order and i > last, i
                    f.write(RECORD.pack(i, p[1].strip()[:10]))
                    n += 1
            if n < 1000:
                raise ValueError("the list came back nearly empty")
            if not in_order:
                data = tmp.read_bytes()
                tmp.write_bytes(b"".join(sorted(data[i:i + RECORD.size] for i in range(0, len(data), RECORD.size))))
            with self._lock:
                os.replace(tmp, self.path)
                self._known.clear()
            log.info("dmr ids: %d kept", n)
            return n
        except Exception as e:
            log.warning("dmr ids: the list couldn't be had (%s)", e)
            tmp.unlink(missing_ok=True)
            return 0

    def _keep_fresh(self) -> None:
        now = self._clock()
        try:
            fresh = now - self.path.stat().st_mtime < STALE_S
        except OSError:
            fresh = False
        if fresh or self._refreshing or now - self._tried < RETRY_S:
            return
        self._refreshing, self._tried = True, now

        def run():
            try:
                self.refresh()
            finally:
                self._refreshing = False
        threading.Thread(target=run, name="dmr-ids", daemon=True).start()

    def callsign(self, number: int | str) -> str | None:
        """The callsign that ID belongs to, if the list has it (and is here yet: it's fetched in the background)."""
        try:
            want = int(number)
        except (TypeError, ValueError):
            return None
        self._keep_fresh()
        with self._lock:
            if want in self._known:
                return self._known[want]
            found = None
            try:
                with open(self.path, "rb") as f:
                    lo, hi = 0, os.fstat(f.fileno()).st_size // RECORD.size
                    while lo < hi:
                        mid = (lo + hi) // 2
                        f.seek(mid * RECORD.size)
                        i, call = RECORD.unpack(f.read(RECORD.size))
                        if i == want:
                            found = call.rstrip(b"\0 ").decode("ascii", errors="replace") or None
                            break
                        lo, hi = (mid + 1, hi) if i < want else (lo, mid)
            except (OSError, struct.error):
                return None                          # (not here yet: asked again next time)
            if len(self._known) > 500:
                self._known.clear()
            self._known[want] = found
            return found


_shared: Ids | None = None


def shared() -> Ids:
    global _shared
    if _shared is None:
        _shared = Ids(Path.home() / ".cache" / "sleepradiopi" / "dmr-ids.bin")
    return _shared
