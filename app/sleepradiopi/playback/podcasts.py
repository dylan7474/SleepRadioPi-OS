"""Podcasts: find shows, keep their episode lists, and choose what to play.

As in the SleepRadio app: shows are found with Apple's free iTunes Search
API (no key), each show's RSS feed gives its episodes (newest first), and a
show on a preset button plays the episode you were part-way through, else
the newest one you haven't heard. Episode lists are cached on the radio, so
they show with no internet (the audio itself needs it). Every episode keeps
its place like an audiobook (the same Positions store, keyed pod:<show>:<id>).

The radio's ffmpeg can't open https, so the station fetches the audio in
Python (playback/radio.py's RadioStream) and asks the server for it from
the right byte to resume or jump (HTTP Range), which works for mp3 -- nearly
every podcast; other formats are read from the start and skipped forward.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlparse

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

DIRECTORY = "https://itunes.apple.com/search"
MAX_FEED_BYTES = 20_000_000
MAX_EPISODES = 200            # kept per show (newest first)
MAX_SHOWS = 50
REFRESH_S = 3 * 3600          # episode lists refreshed in the background this often
DONE_MARGIN_MS = 60_000       # within a minute of the end counts as heard


class PodcastError(ValueError):
    """Couldn't find, fetch or read a show: the text is for the page."""


def feed_id(url: str) -> str:
    """A stable id for a show's feed (the same feed found two ways matches)."""
    norm = url.strip().rstrip("/").lower()
    return "pod_" + hashlib.sha1(norm.encode()).hexdigest()[:12]


def _get(url: str, limit: int, headers: dict | None = None, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": radio_mod.USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def search(query: str, fetch=_get) -> list[dict]:
    """Shows whose title matches (the iTunes directory, as the app searches it)."""
    q = query.strip()
    if not q:
        return []
    try:
        data = json.loads(fetch(f"{DIRECTORY}?media=podcast&entity=podcast&limit=30&term={quote(q)}", 2_000_000,
                                {"Accept": "application/json"}))
    except (OSError, ValueError) as e:
        log.warning("podcast search %r failed: %s", q, e)
        raise PodcastError("couldn't reach the podcast directory (is the radio online?)") from None
    out, seen = [], set()
    for o in data.get("results", []) if isinstance(data, dict) else []:
        url = str(o.get("feedUrl") or "").strip()
        title = str(o.get("collectionName") or o.get("trackName") or "").strip()
        if not url or not title or url in seen or urlparse(url).scheme not in ("http", "https"):
            continue
        seen.add(url)
        out.append({"id": feed_id(url), "feed_url": url, "title": title[:200],
                    "author": str(o.get("artistName") or "").strip()[:200]})
    return out


# --- the feed ---------------------------------------------------------------------------

def _local(tag: str) -> tuple[str, str]:
    """('itunes' | '', local name) for an element's tag."""
    if tag.startswith("{"):
        ns, name = tag[1:].split("}", 1)
        return ("itunes" if "itunes" in ns else "ns"), name
    return "", tag


def _child(el, name: str, itunes: bool = False):
    for c in el:
        ns, local = _local(c.tag)
        if local == name and (ns == "itunes") == itunes:
            return c
    return None


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_duration(raw: str) -> int:
    """itunes:duration ('1:02:03', '62:03' or seconds) -> ms; 0 if unknown."""
    raw = (raw or "").strip()
    try:
        parts = [float(p) for p in raw.split(":")]
    except ValueError:
        return 0
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return int(secs * 1000) if 0 < secs < 48 * 3600 else 0


def parse_date(raw: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(raw.strip()).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0.0


def parse_feed(xml: bytes, url: str = "") -> dict:
    """An RSS feed -> {"title", "author", "episodes": [{guid, title, url, type,
    pub, duration_ms, about}]} newest first. Episodes without audio are skipped."""
    head = xml[:4096].decode("utf-8", errors="replace")
    if "<!ENTITY" in head:
        raise PodcastError("that feed has odd XML in it; the radio won't read it")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise PodcastError("that isn't a podcast feed") from None
    channel = root.find("channel")
    if channel is None:
        raise PodcastError("that isn't a podcast feed")
    title = _text(_child(channel, "title")) or urlparse(url).netloc
    author = _text(_child(channel, "author", itunes=True))
    episodes = []
    for item in channel.findall("item"):
        enc = _child(item, "enclosure")
        audio = (enc.get("url") or "").strip() if enc is not None else ""
        etitle = _text(_child(item, "title"))
        if not audio or not etitle or urlparse(audio).scheme not in ("http", "https"):
            continue
        about = re.sub(r"<[^>]+>", " ", _text(_child(item, "description")) or _text(_child(item, "summary", True)))
        episodes.append({
            "guid": (_text(_child(item, "guid")) or audio)[:500],
            "title": etitle[:300],
            "url": audio,
            "type": (enc.get("type") or "").lower(),
            "bytes": int(enc.get("length") or 0) if (enc.get("length") or "").isdigit() else 0,
            "pub": parse_date(_text(_child(item, "pubDate"))),
            "duration_ms": parse_duration(_text(_child(item, "duration", itunes=True))),
            "about": " ".join(about.split())[:400],
        })
    episodes.sort(key=lambda e: -e["pub"])
    return {"title": title[:200], "author": author[:200], "episodes": episodes[:MAX_EPISODES]}


def guid_id(guid: str) -> str:
    """A short, stable id for an episode (feeds' own ids can be long web addresses)."""
    return hashlib.sha1(guid.encode()).hexdigest()[:16]


def episode_key(show_id: str, guid: str) -> str:
    return f"pod:{show_id}:{guid_id(guid)}"


def is_mp3(ep: dict) -> bool:
    return "mpeg" in ep.get("type", "") or urlparse(ep["url"]).path.lower().endswith(".mp3")


class Podcasts:
    """The shows subscribed to (in the settings), their cached episode lists,
    and which episode to play."""

    def __init__(self, shows: list | None, cache_dir: Path | None, positions, save=None, fetch=_get) -> None:
        self.shows: list[dict] = validate_shows(shows or [])
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.positions = positions            # audiobooks.Positions: {key: {"pos_ms", "at", "done"}}
        self.save = save                      # save(shows): writes them to the settings
        self.fetch = fetch
        self._feeds: dict[str, dict] = {}
        self._lock = threading.Lock()

    # --- shows ---------------------------------------------------------------------------

    def show(self, show_id: str) -> dict | None:
        return next((s for s in self.shows if s["id"] == show_id), None)

    def subscribe(self, feed_url: str) -> dict:
        if len(self.shows) >= MAX_SHOWS:
            raise PodcastError(f"at most {MAX_SHOWS} podcasts")
        if urlparse(feed_url.strip()).scheme not in ("http", "https"):
            raise PodcastError("a feed's address starts http:// or https://")
        sid = feed_id(feed_url)
        feed = self.refresh(sid, feed_url.strip())            # (checks it's a feed)
        show = {"id": sid, "feed_url": feed_url.strip(), "title": feed["title"], "author": feed["author"]}
        with self._lock:
            self.shows = [s for s in self.shows if s["id"] != sid] + [show]
            shows = list(self.shows)
        if self.save:
            self.save(shows)
        log.info("podcasts: subscribed to %s", show["title"])
        return show

    def unsubscribe(self, show_id: str) -> None:
        with self._lock:
            self.shows = [s for s in self.shows if s["id"] != show_id]
            shows = list(self.shows)
        if self.save:
            self.save(shows)

    # --- episode lists ----------------------------------------------------------------------

    def _cache(self, sid: str) -> Path | None:
        return self.cache_dir / f"{sid}.json" if self.cache_dir is not None else None

    def feed(self, sid: str) -> dict | None:
        """The show's episode list: in memory, else the cache on the radio."""
        with self._lock:
            if sid in self._feeds:
                return self._feeds[sid]
        path = self._cache(sid)
        try:
            feed = json.loads(path.read_text()) if path else None
        except (OSError, ValueError):
            feed = None
        if feed is not None:
            with self._lock:
                self._feeds[sid] = feed
        return feed

    def refresh(self, sid: str, url: str | None = None) -> dict:
        url = url or (self.show(sid) or {}).get("feed_url")
        if not url:
            raise PodcastError("that podcast isn't subscribed to")
        try:
            data = self.fetch(url, MAX_FEED_BYTES)
        except (OSError, ValueError) as e:
            cached = self.feed(sid)
            if cached is not None:
                log.info("podcasts: %s: using the saved list (%s)", sid, e)
                return cached
            raise PodcastError("couldn't reach that podcast (is the radio online?)") from None
        feed = parse_feed(data, url)
        feed["fetched"] = time.time()
        with self._lock:
            self._feeds[sid] = feed
        path = self._cache(sid)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(path, json.dumps(feed))
            except OSError:
                pass
        return feed

    def refresh_all(self) -> None:
        for s in list(self.shows):
            try:
                self.refresh(s["id"])
            except Exception as e:
                log.info("podcasts: %s not refreshed: %s", s["title"], e)

    def keep_fresh(self, start_delay_s: float = 180) -> None:
        def run():
            time.sleep(start_delay_s)
            while True:
                self.refresh_all()
                time.sleep(REFRESH_S)
        threading.Thread(target=run, name="podcasts", daemon=True).start()

    # --- places ---------------------------------------------------------------------------

    def progress(self, sid: str, ep: dict) -> dict:
        key = episode_key(sid, ep["guid"])
        pos = self.positions.get(key)
        length = ep["duration_ms"]
        done = self.positions.done(key) or (length and pos >= length - min(DONE_MARGIN_MS, length // 10))
        return {"pos_ms": pos, "done": bool(done), "at": self.positions.when(key)}

    def episodes(self, sid: str) -> list[dict]:
        feed = self.feed(sid) or {"episodes": []}
        return [{**e, **self.progress(sid, e), "gid": guid_id(e["guid"])} for e in feed["episodes"]]

    def pick(self, sid: str, start_guid: str | None = None) -> dict | None:
        """What a podcast button plays. Given the episode it starts from: the
        first episode from there (oldest to newest) not yet heard, carrying on
        from its place -- the radio then steps on through the newer ones up to
        the latest. Without one (as the app): the most recently played
        unfinished episode, else the newest not heard, else the newest."""
        eps = self.episodes(sid)
        if not eps:
            return None
        if start_guid:
            oldest_first = eps[::-1]
            at = next((i for i, e in enumerate(oldest_first) if start_guid in (e["gid"], e["guid"])), None)
            if at is not None:
                return next((e for e in oldest_first[at:] if not e["done"]), oldest_first[-1])
        going = [e for e in eps if not e["done"] and e["pos_ms"] > 0]
        if going:
            return max(going, key=lambda e: e["at"])
        return next((e for e in eps if not e["done"]), eps[0])

    def next_after(self, sid: str, guid: str) -> dict | None:
        """The next newer episode (None after the latest)."""
        eps = self.episodes(sid)                      # newest first
        i = next((i for i, e in enumerate(eps) if e["guid"] == guid), None)
        return eps[i - 1] if i else None

    def summary(self) -> list[dict]:
        out = []
        for s in self.shows:
            eps = self.episodes(s["id"])
            latest = eps[0] if eps else None
            out.append({**s, "episodes": len(eps), "new": sum(1 for e in eps[:20] if not e["done"] and not e["pos_ms"]),
                        "latest": {k: latest[k] for k in ("title", "pub", "duration_ms")} if latest else None,
                        "playing_at": max((e["at"] for e in eps), default=0)})
        return out


def validate_shows(shows) -> list[dict]:
    if not isinstance(shows, list) or len(shows) > MAX_SHOWS:
        raise ValueError(f"send up to {MAX_SHOWS} podcasts")
    out = []
    for s in shows:
        if not isinstance(s, dict) or not isinstance(s.get("feed_url"), str) \
                or urlparse(s["feed_url"]).scheme not in ("http", "https"):
            raise ValueError("a podcast needs its feed's address")
        out.append({"id": feed_id(s["feed_url"]), "feed_url": s["feed_url"].strip(),
                    "title": str(s.get("title") or s["feed_url"])[:200], "author": str(s.get("author") or "")[:200]})
    return out
