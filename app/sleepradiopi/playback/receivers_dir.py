"""Find a receiver: the public directory of internet software radios.

receiverbook.de keeps a map of the OpenWebRX and KiwiSDR receivers that are
on line -- over a thousand, each with where it is, what its owner says about
it and its address. Its page carries the whole list (`var receivers = [...]`),
so one fetch a day is the directory: kept in a file, searched here by words
and by distance. WebSDRs are on the map too but can't be played without a
browser, so they're left out.

info() asks one receiver about itself -- an OpenWebRX its bands, a KiwiSDR
how many listeners it has room for -- but only a receiver that's in the
directory: the radio doesn't fetch addresses a web page hands it.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

SOURCE = "https://www.receiverbook.de/map"
STALE_S = 24 * 3600
USER_AGENT = "Mozilla/5.0 (X11; Linux) SleepRadio"
KINDS = {"KiwiSDR": "kiwi", "OpenWebRX": "owrx"}
MAX_RESULTS = 60


def _get_text(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8_000_000).decode("utf-8", errors="replace")


def _clean(text) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", " ", str(text or "")))
    return re.sub(r"\s+", " ", text).strip()[:160]


def parse_map(page: str) -> list[dict]:
    """The map page -> [{"name", "place", "url", "kind", "lat", "lon", "version"}], each address once."""
    m = re.search(r"var\s+receivers\s*=\s*(\[.*\]);", page)
    if not m:
        raise ValueError("the directory's page has changed: no list of receivers in it")
    out, seen = [], set()
    for site in json.loads(m.group(1)):
        try:
            lon, lat = (float(v) for v in site["location"]["coordinates"][:2])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        for r in site.get("receivers") or []:
            kind, url = KINDS.get(r.get("type")), str(r.get("url") or "")
            if kind is None or not url.startswith(("http://", "https://")) or url in seen or len(url) > 300:
                continue
            seen.add(url)
            out.append({"name": _clean(r.get("label")) or url, "place": _clean(site.get("label")), "url": url if url.endswith("/") else url + "/",
                        "kind": kind, "lat": round(lat, 4), "lon": round(lon, 4), "version": _clean(r.get("version"))[:20]})
    return out


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


class ReceiverDirectory:
    def __init__(self, path: Path, fetch: Callable[[str], str] = _get_text, clock: Callable[[], float] = time.time) -> None:
        self.path, self._fetch, self._clock = path, fetch, clock
        self.receivers: list[dict] = []
        self.at = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._refreshing = False
        try:
            data = json.loads(path.read_text())
            self.receivers, self.at = data["receivers"], float(data["at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def status(self) -> dict:
        return {"count": len(self.receivers), "age_s": round(self._clock() - self.at) if self.at else None, "error": self.error}

    def refresh(self) -> int:
        try:
            found = parse_map(self._fetch(SOURCE))
            if len(found) < 20:
                raise ValueError("the directory came back nearly empty")
        except Exception as e:
            self.error = str(e) if isinstance(e, ValueError) else f"the directory couldn't be reached ({e})"
            log.warning("receivers: %s", self.error)
            return 0
        with self._lock:
            self.receivers, self.at, self.error = found, self._clock(), None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.path, json.dumps({"at": self.at, "receivers": found}))
        except OSError:
            log.exception("receivers: couldn't keep the directory")
        log.info("receivers: %d in the directory", len(found))
        return len(found)

    def _ensure(self) -> None:
        """Have a list: fetched now if there's none, in the background if it's a day old."""
        if not self.receivers:
            self.refresh()
        elif self._clock() - self.at > STALE_S and not self._refreshing:
            self._refreshing = True

            def run():
                try:
                    self.refresh()
                finally:
                    self._refreshing = False
            threading.Thread(target=run, name="receiver-directory", daemon=True).start()

    def search(self, q: str = "", kind: str = "", near: tuple[float, float] | None = None, limit: int = MAX_RESULTS) -> list[dict]:
        """Receivers whose name, place or address has every word of q; the
        nearest to `near` (lat, lon) first if it's given, each with its "km"."""
        self._ensure()
        words = [w for w in q.lower().split() if w]
        found = []
        for r in self.receivers:
            if kind and r["kind"] != kind:
                continue
            hay = f"{r['name']} {r['place']} {r['url']}".lower()
            if all(w in hay for w in words):
                found.append({**r, "km": round(km_between(near[0], near[1], r["lat"], r["lon"]))} if near else dict(r))
        found.sort(key=(lambda r: r["km"]) if near else (lambda r: (r["place"].lower(), r["name"].lower())))
        return found[:max(1, min(limit, MAX_RESULTS))]

    def info(self, url: str, fetch: Callable[[str], str] | None = None) -> dict:
        """What a receiver in the directory says about itself right now. ValueError says why not."""
        r = next((x for x in self.receivers if x["url"] == url), None)
        if r is None:
            raise ValueError("that receiver isn't in the directory")
        get = fetch or (lambda u: _get_text(u, timeout=8.0))
        try:
            if r["kind"] == "kiwi":
                st = dict(line.split("=", 1) for line in get(url + "status").splitlines() if "=" in line)
                num = lambda k: int(st[k]) if st.get(k, "").lstrip("-").isdigit() else None
                lo, _, hi = st.get("bands", "0-30000000").partition("-")
                return {**r, "title": _clean(st.get("name")) or r["name"], "users": num("users"), "users_max": num("users_max"),
                        "antenna": _clean(st.get("antenna")), "offline": st.get("offline") == "yes" or st.get("status") not in (None, "active"),
                        "low_khz": int(lo) // 1000 if lo.isdigit() else 0, "high_khz": int(hi) // 1000 if hi.isdigit() else 30000}
            st = json.loads(get(url + "status.json"))
            bands = [{"name": f"{_clean(p.get('name'))}"[:80], "centre": int(p["center_freq"]), "width": int(p["sample_rate"])}
                     for sdr in st.get("sdrs") or [] for p in sdr.get("profiles") or []
                     if isinstance(p.get("center_freq"), (int, float)) and isinstance(p.get("sample_rate"), (int, float))
                     and 0 < p["center_freq"] < 3e9]
            return {**r, "title": _clean((st.get("receiver") or {}).get("name")) or r["name"], "max_clients": st.get("max_clients"),
                    "bands": sorted(bands, key=lambda b: b["centre"])}
        except ValueError:
            raise ValueError("that receiver's answer couldn't be read") from None
        except Exception as e:
            raise ValueError(f"that receiver isn't answering ({str(e)[:60]})") from None


_shared: ReceiverDirectory | None = None


def shared() -> ReceiverDirectory:
    """The radio's one copy (kept in ~/.cache/sleepradiopi/receivers.json)."""
    global _shared
    if _shared is None:
        _shared = ReceiverDirectory(Path.home() / ".cache" / "sleepradiopi" / "receivers.json")
    return _shared
