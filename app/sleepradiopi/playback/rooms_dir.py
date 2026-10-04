"""Find a room: the public register of System Fusion reflectors (see room.py).

Reflector owners register them at register.ysfreflector.de; Pi-Star keeps a
copy as a plain list (one a line: id;name;description;host;port;...), which is
what's fetched here, once a day, kept in a file and searched by words. The
rooms of the FCS network (where most WIRES-X rooms are bridged) are in a
second list beside it (FCS00290;name;description;;;), and are found too.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback import room

log = logging.getLogger(__name__)

SOURCE = "https://www.pistar.uk/downloads/YSF_Hosts.txt"
FCS_SOURCE = "https://www.pistar.uk/downloads/FCS_Hosts.txt"
STALE_S = 24 * 3600
RETRY_S = 600
MAX_RESULTS = 60


def _get_text(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux) SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", errors="replace")


def parse_hosts(text: str) -> list[dict]:
    """The list -> [{"id", "name", "about", "url"}], each address once."""
    out, seen = [], set()
    for line in text.splitlines():
        p = [x.strip() for x in line.split(";")]
        if line.startswith("#") or len(p) < 5 or not p[1] or not p[3] or not p[4].isdigit() or not 0 < int(p[4]) < 65536:
            continue
        if any(ch in p[3] for ch in "/ @?#") or len(p[3]) > 100:
            continue
        url = room.link(p[3], int(p[4]), p[1][:40])
        if (p[3], p[4]) in seen:
            continue
        seen.add((p[3], p[4]))
        out.append({"id": p[0][:8], "name": p[1][:40], "about": p[2][:60], "url": url})
    return out


def parse_fcs(text: str) -> list[dict]:
    """The FCS list -> the same, for the rooms that have a name."""
    out, seen = [], set()
    for line in text.splitlines():
        p = [x.strip() for x in line.split(";")]
        rid = p[0].upper()
        if len(p) < 2 or len(rid) != 8 or not rid.startswith("FCS") or not rid[3:].isdigit() or not p[1] or rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "name": p[1][:40], "about": (p[2] if len(p) > 2 and p[2] else "FCS")[:60], "url": room.fcs_link(rid, p[1][:40])})
    return out


class RoomDirectory:
    def __init__(self, path: Path, fetch: Callable[[str], str] = _get_text, clock: Callable[[], float] = time.time) -> None:
        self.path, self._fetch, self._clock = path, fetch, clock
        self.rooms: list[dict] = []
        self.at = 0.0
        self.error: str | None = None
        self._refreshing = False
        self._tried = -1e9
        try:
            data = json.loads(path.read_text())
            self.rooms, self.at = data["rooms"], float(data["at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def status(self) -> dict:
        return {"count": len(self.rooms), "age_s": round(self._clock() - self.at) if self.at else None, "error": self.error}

    def refresh(self) -> int:
        try:
            found = parse_hosts(self._fetch(SOURCE))
            if len(found) < 20:
                raise ValueError("the register came back nearly empty")
            try:
                found += parse_fcs(self._fetch(FCS_SOURCE))
            except Exception as e:                    # (the YSF rooms alone are still worth having)
                log.warning("rooms: no FCS list (%s)", e)
        except Exception as e:
            self.error = str(e) if isinstance(e, ValueError) else f"the register couldn't be reached ({e})"
            log.warning("rooms: %s", self.error)
            return 0
        self.rooms, self.at, self.error = found, self._clock(), None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.path, json.dumps({"at": self.at, "rooms": found}))
        except OSError:
            log.exception("rooms: couldn't keep the register")
        return len(found)

    def search(self, q: str = "", limit: int = MAX_RESULTS) -> list[dict]:
        """Rooms whose name, description or number has every word of q, by name."""
        # (a list kept from before the FCS rooms were in it is fetched again, but not on every search if that fails)
        lacking = not any(r["id"].startswith("FCS") for r in self.rooms) and self._clock() - self._tried > RETRY_S
        if not self.rooms:
            self.refresh()
        elif (self._clock() - self.at > STALE_S or lacking) and not self._refreshing:
            self._refreshing = True
            self._tried = self._clock()

            def run():
                try:
                    self.refresh()
                finally:
                    self._refreshing = False
            threading.Thread(target=run, name="room-directory", daemon=True).start()
        words = q.lower().split()
        found = [r for r in self.rooms if all(w in f"{r['name']} {r['about']} {r['id']}".lower() for w in words)]
        return sorted(found, key=lambda r: r["name"].lower())[:max(1, min(limit, MAX_RESULTS))]


_shared: RoomDirectory | None = None


def shared() -> RoomDirectory:
    global _shared
    if _shared is None:
        _shared = RoomDirectory(Path.home() / ".cache" / "sleepradiopi" / "rooms.json")
    return _shared
