"""Internet radio: play a station's stream on the radio, and find stations.

Python fetches the stream -- over http or https (the image's ffmpeg has no
TLS) -- and pipes the audio bytes to ffmpeg, which decodes them to the
station's PCM (16-bit stereo, 44.1 kHz). On the way it:

  * reads the now-playing titles Icecast/Shoutcast stations put in the
    stream (ICY metadata), so the page can show what's on;
  * follows .pls / .m3u playlists to the stream inside;
  * plays HLS (.m3u8, e.g. the BBC) by fetching its segments in turn.

Stations are found with the Radio Browser directory (radio-browser.info),
as the Android app does; the starter set is the Android app's. Its servers
come and go (all.api... often points at one that's down), so the radio keeps
a compact copy of the whole directory (Directory: ~50,000 stations, ~6 MB,
refreshed weekly in the background, trying each mirror in turn) and searches
that: instant, and it works when the servers don't.
"""

from __future__ import annotations

import csv
import http.client
import io
import json
import logging
import os
import time
import math
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

# The app's round-robin name first, then mirrors by name (all.api... can land
# on one that's down). The first that answers is tried first next time.
MIRRORS = ["https://all.api.radio-browser.info", "https://de1.api.radio-browser.info",
           "https://de2.api.radio-browser.info"]
MIRROR_TIMEOUT_S = 8.0
REFRESH_S = 7 * 24 * 3600    # the directory copy is refreshed weekly
RETRY_S = 3600               # ...or an hour after a failed try
PAGE = 10_000                # stations per request (the servers drop long downloads)
PAGE_TRIES = 4
USER_AGENT = "SleepRadioPi/1.0 (+https://github.com/dylan7474/SleepRadioPi-OS)"
TIMEOUT_S = 10.0
MAX_STATIONS = 100
MAX_NAME = 120
MAX_URL = 2000
QUEUE_BLOCKS = 60            # ~5.6 s of decoded audio waiting for the show
PLAYLIST_BYTES = 256_000     # a playlist bigger than this isn't one
MAX_HOPS = 5                 # playlists pointing at playlists
HLS_LIVE_EDGE = 3            # start this many segments back from the newest
HLS_MAX_BANDWIDTH = 200_000  # prefer the best variant up to this (a Zero's Wi-Fi is weak)
HLS_TRIES = 3                # fetches of a segment / playlist before giving up on it (the BBC's
HLS_RETRY_S = 0.5            #  https edge drops ~1 connection in 10); waits 0.5 s, then 1 s
HLS_MAX_MISSES = 3           # segments / playlist refreshes lost in a row before the stream is dead
DECODER = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-probesize", "16384", "-analyzeduration", "1000000", "-i", "pipe:0",
           "-vn", "-ac", str(pcm.CHANNELS), "-ar", str(pcm.SAMPLE_RATE), "-f", "s16le", "pipe:1"]

# The Android app's starter set (SourceModels.kt), at bitrates kinder to a
# Zero's Wi-Fi. BBC HLS "pool" ids change now and then: if Radio 4 stops,
# find it again with the search.
DEFAULT_STATIONS = [
    {"name": "BBC Radio 4", "info": "Speech, news & drama",
     "url": "http://as-hls-ww-live.akamaized.net/pool_55057080/live/ww/bbc_radio_fourfm/"
            "bbc_radio_fourfm.isml/bbc_radio_fourfm-audio=128000.norewind.m3u8"},
    {"name": "BBC World Service", "info": "International news",
     "url": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"},
    {"name": "Radio Paradise", "info": "Eclectic DJ-curated mix",
     "url": "http://stream.radioparadise.com/aac-128"},
    {"name": "Radio Paradise — Mellow", "info": "Softer, downtempo",
     "url": "http://stream.radioparadise.com/mellow-128"},
    {"name": "SomaFM — Groove Salad", "info": "Ambient / downtempo",
     "url": "http://ice1.somafm.com/groovesalad-128-mp3"},
    {"name": "SomaFM — Drone Zone", "info": "Atmospheric textures for sleep",
     "url": "http://ice1.somafm.com/dronezone-128-mp3"},
]


class StreamError(Exception):
    """A station couldn't be reached or played, or the directory search failed."""


# --- stations ---------------------------------------------------------------------------

def validate_station(d) -> dict:
    """{"name", "url", "info"?} -> a clean copy; ValueError if it isn't one."""
    if not isinstance(d, dict):
        raise ValueError("a station is {\"name\", \"url\"}")
    name, url, info = d.get("name"), d.get("url"), d.get("info", "")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        raise ValueError(f"a station needs a name (up to {MAX_NAME} letters)")
    parts = urlparse(url.strip()) if isinstance(url, str) else None
    # (ysf:// and fcs://: a room of radio amateurs' digital voice -- playback/room.py)
    if parts is None or len(url) > MAX_URL or parts.scheme not in ("http", "https", "ysf", "fcs") or not parts.netloc:
        raise ValueError(f"{name.strip()}: the address must start http:// or https://")
    if not isinstance(info, str):
        info = ""
    return {"name": name.strip(), "url": url.strip(), "info": info.strip()[:MAX_NAME]}


def validate_stations(stations) -> list[dict]:
    """The saved list: stations, each address once, at most MAX_STATIONS."""
    if not isinstance(stations, list):
        raise ValueError("send a list of stations")
    if len(stations) > MAX_STATIONS:
        raise ValueError(f"at most {MAX_STATIONS} stations")
    out, seen = [], set()
    for s in stations:
        s = validate_station(s)
        if s["url"] not in seen:
            seen.add(s["url"])
            out.append(s)
    return out


def _get(url: str, headers: dict | None = None, timeout: float = TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _mirrored(path: str, fetch: Callable, headers: dict | None = None, timeout: float = MIRROR_TIMEOUT_S):
    """Open path on the first Radio Browser mirror that answers (and try that
    one first next time). StreamError if none does."""
    last = None
    for base in list(MIRRORS):
        try:
            resp = fetch(base + path, headers=headers, timeout=timeout)
        except (OSError, ValueError) as e:     # URLError, timeouts, HTTP errors
            log.info("station directory: %s didn't answer (%s)", base, e)
            last = e
            continue
        if MIRRORS[0] != base:
            MIRRORS.remove(base)
            MIRRORS.insert(0, base)
        return resp
    log.warning("station directory: no mirror answered (%s)", last)
    raise StreamError("couldn't reach the station directory (is the radio online?)")


def search(query: str, limit: int = 40, fetch: Callable = _get) -> list[dict]:
    """Stations whose name matches, best known first (Radio Browser online, as
    the Android app searches it). StreamError if the directory can't be reached."""
    q = query.strip()
    if not q:
        return []
    path = (f"/json/stations/search?name={quote(q)}&limit={int(limit)}"
            "&hidebroken=true&order=clickcount&reverse=true")
    try:
        with _mirrored(path, fetch, {"Accept": "application/json"}) as r:
            rows = json.loads(r.read(4_000_000))
    except (OSError, ValueError) as e:
        log.warning("station search %r failed: %s", q, e)
        raise StreamError("couldn't reach the station directory (is the radio online?)") from None
    out, seen = [], set()
    for o in rows if isinstance(rows, list) else []:
        if not isinstance(o, dict):
            continue
        stream = str(o.get("url_resolved") or o.get("url") or "").strip()
        name = str(o.get("name") or "").strip()
        codec = str(o.get("codec") or "").strip()
        bitrate = o.get("bitrate") if isinstance(o.get("bitrate"), int) else 0
        country = str(o.get("country") or "").strip()
        info = " · ".join(x for x in (codec if codec.upper() != "UNKNOWN" else "",
                                      f"{bitrate}k" if bitrate > 0 else "", country) if x)
        try:
            s = validate_station({"name": name[:MAX_NAME], "url": stream, "info": info or "Internet radio"})
        except ValueError:
            continue
        if s["url"] not in seen:
            seen.add(s["url"])
            out.append(s)
    return out


class Directory:
    """A copy of the whole Radio Browser directory on the radio, searched
    locally. One station per line, most listened-to first:
    name, stream address, codec, bitrate, country, clicks, tags (tab-separated).
    Until the first copy is made (or if it can't be), searches go online."""

    FIELDS = ("name", "url", "codec", "bitrate", "country", "clicks", "tags")

    def __init__(self, path: Path, fetch: Callable = _get) -> None:
        self.path = path
        self.fetch = fetch
        self.refreshing = False
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        try:
            meta = json.loads(self.path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            meta = {}
        return {"stations": meta.get("stations", 0) if self.path.is_file() else 0,
                "updated": meta.get("updated") if self.path.is_file() else None,
                "refreshing": self.refreshing, "error": self.last_error}

    def age_s(self) -> float | None:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return None

    def refresh(self) -> int:
        """Download the directory (~35 MB of CSV, read as it arrives, a page at
        a time: the servers drop long downloads) and keep the working stations.
        Returns how many. StreamError if it can't."""
        with self._lock:
            if self.refreshing:
                raise StreamError("already being fetched")
            self.refreshing = True
        tmp = self.path.with_suffix(".tmp")
        try:
            t0 = time.monotonic()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            n, seen = 0, set()
            with open(tmp, "w", encoding="utf-8") as out:
                offset = 0
                while True:
                    lines, rows = self._page(offset)
                    for line in lines:
                        url = line.split("\t", 2)[1]
                        if url not in seen:          # (a station can move between pages)
                            seen.add(url)
                            out.write(line + "\n")
                            n += 1
                    if rows == 0 or offset > 100 * PAGE:   # (a short page isn't the end: the
                        break                              #  servers sometimes stop early)
                    offset += rows
                out.flush()
                os.fsync(out.fileno())
            if n < 1000:                        # a cut-off download: keep the old copy
                raise StreamError(f"the directory came back with only {n} stations")
            os.replace(tmp, self.path)
            meta = self.path.with_suffix(".json")
            meta.write_text(json.dumps({"stations": n, "updated": time.time()}))
            self.last_error = None
            log.info("station directory: %d stations saved in %.0f s", n, time.monotonic() - t0)
            return n
        except (OSError, ValueError, csv.Error, http.client.HTTPException, StreamError) as e:
            self.last_error = str(e) if isinstance(e, StreamError) else _reason(e)
            log.warning("station directory: refresh failed: %s", e)
            raise StreamError(self.last_error) from None
        finally:
            self.refreshing = False
            try:
                tmp.unlink()
            except OSError:
                pass

    def _page(self, offset: int) -> tuple[list[str], int]:
        """One page of the directory: (its lines for the copy, rows read), trying
        again (on each mirror) if the download breaks off."""
        path = (f"/csv/stations/search?hidebroken=true&order=clickcount&reverse=true"
                f"&offset={offset}&limit={PAGE}")
        for attempt in range(PAGE_TRIES):
            try:
                with _mirrored(path, self.fetch, timeout=30) as resp:
                    lines, rows = [], 0
                    for row in csv.DictReader(io.TextIOWrapper(resp, encoding="utf-8", errors="replace",
                                                               newline="")):
                        rows += 1
                        if line := _directory_line(row):
                            lines.append(line)
                return lines, rows
            except (OSError, csv.Error, http.client.HTTPException) as e:
                if attempt == PAGE_TRIES - 1:
                    raise
                log.info("station directory: page at %d broke off (%s); again", offset, e)
                time.sleep(2 + 3 * attempt)
        raise StreamError("unreachable")

    def keep_fresh(self, start_delay_s: float = 120.0) -> None:
        """Refresh when there's no copy or it's a week old (a background thread,
        at low priority, starting after the show has had time to get going)."""
        def run():
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (AttributeError, OSError):
                pass
            time.sleep(start_delay_s)
            while True:
                age = self.age_s()
                if age is None or age > REFRESH_S:
                    try:
                        self.refresh()
                    except Exception:               # (logged in refresh); never let the thread die
                        time.sleep(RETRY_S)
                        continue
                time.sleep(min(REFRESH_S, RETRY_S * 6))
        threading.Thread(target=run, name="station-directory", daemon=True).start()

    def search(self, query: str, limit: int = 40) -> tuple[list[dict], str]:
        """(stations, "copy" | "online"): every word in the name (or its tags),
        most listened-to first."""
        words = query.lower().split()
        if not words:
            return [], "copy"
        if not self.path.is_file():
            return search(query, limit, self.fetch), "online"
        by_name, by_tag = [], []
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(self.FIELDS):
                    continue
                name = parts[0].lower()
                if all(w in name for w in words) and _starts_words(words, name):
                    by_name.append(parts)
                    if len(by_name) >= limit:
                        break
                elif len(by_tag) < limit and all(w in name or w in parts[6].lower() for w in words) \
                        and _starts_words(words, f"{name} {parts[6].lower()}"):
                    by_tag.append(parts)
        out, seen = [], set()
        for p in (by_name + by_tag):
            if p[1] in seen:
                continue
            seen.add(p[1])
            info = " · ".join(x for x in (p[2] if p[2].upper() != "UNKNOWN" else "",
                                          f"{p[3]}k" if p[3] not in ("", "0") else "", p[4]) if x)
            out.append({"name": p[0], "url": p[1], "info": info or "Internet radio"})
            if len(out) >= limit:
                break
        return out, "copy"


def _starts_words(words: list[str], text: str) -> bool:
    """Every query word starts a word of text ("radio 4": not "Radio 24")."""
    tokens = re.split(r"[^\w]+", text)
    return all(any(t.startswith(w) for t in tokens) for w in words)


def _directory_line(row: dict) -> str | None:
    """A Radio Browser CSV row -> one line of the copy (None: skip it)."""
    if row.get("lastcheckok") != "1":
        return None
    url = (row.get("url_resolved") or row.get("url") or "").strip()
    name = " ".join((row.get("name") or "").split())[:MAX_NAME]
    if not name or not url.startswith(("http://", "https://")) or len(url) > MAX_URL or "\t" in url:
        return None
    clean = lambda x, n: " ".join((x or "").split())[:n]
    bitrate = row.get("bitrate") or "0"
    return "\t".join([name, url, clean(row.get("codec"), 10), bitrate if bitrate.isdigit() else "0",
                      clean(row.get("country"), 40), clean(row.get("clickcount"), 10),
                      clean(row.get("tags"), 100)])


# --- playlists and HLS -------------------------------------------------------------------

def playlist_urls(text: str, base: str) -> list[str]:
    """The stream addresses in a .pls or .m3u playlist."""
    urls = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(?i)file\d+\s*=\s*(.+)", line)       # .pls
        if m:
            line = m.group(1).strip()
        elif not line or line.startswith(("#", "[")) or re.match(r"(?i)\w+\s*=", line):
            continue
        url = urljoin(base, line)
        if urlparse(url).scheme in ("http", "https"):
            urls.append(url)
    return urls


def is_hls(text: str) -> bool:
    return "#EXT-X-TARGETDURATION" in text or "#EXT-X-STREAM-INF" in text


def hls_variant(text: str, base: str) -> str | None:
    """A master playlist's best variant up to HLS_MAX_BANDWIDTH (else its smallest);
    None if this is already a media playlist."""
    variants, bw = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"[:,]BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
        elif line and not line.startswith("#") and bw is not None:
            variants.append((bw, urljoin(base, line)))
            bw = None
    if not variants:
        return None
    fits = [v for v in variants if v[0] <= HLS_MAX_BANDWIDTH]
    return max(fits)[1] if fits else min(variants)[1]


def hls_media(text: str, base: str) -> dict:
    """A media playlist: {"seq", "target", "segments": [url], "map", "encrypted", "ended"}."""
    out = {"seq": 0, "target": 6.0, "segments": [], "map": None, "encrypted": False, "ended": False}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            out["seq"] = int(line.split(":", 1)[1] or 0)
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            out["target"] = max(1.0, float(line.split(":", 1)[1] or 6))
        elif line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            out["map"] = urljoin(base, m.group(1)) if m else None
        elif line.startswith("#EXT-X-KEY:"):
            out["encrypted"] = "METHOD=NONE" not in line
        elif line.startswith("#EXT-X-ENDLIST"):
            out["ended"] = True
        elif line and not line.startswith("#"):
            out["segments"].append(urljoin(base, line))
    return out


def icy_title(meta: bytes) -> str | None:
    """StreamTitle='Artist - Title'; from an ICY metadata block."""
    text = meta.rstrip(b"\0").decode("utf-8", errors="replace")
    m = re.search(r"StreamTitle='(.*?)';", text, re.S)
    return m.group(1).strip() if m else None


# --- levelling ------------------------------------------------------------------------

class Leveller:
    """A slow automatic gain, so stations sit near the show's music level
    (pcm.LOUDNESS_TARGET_RMS, ~-19 dBFS) instead of jumping out at you: the
    loudness is measured over several seconds and the gain follows it gently."""

    def __init__(self, target: float = pcm.LOUDNESS_TARGET_RMS, tau_s: float = 8.0,
                 lo: float = 0.25, hi: float = 2.0) -> None:
        self.target, self.tau_s, self.lo, self.hi = target, tau_s, lo, hi
        self.mean_sq: float | None = None
        self.gain = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        n = len(block)
        if n == 0:
            return block
        x = block.astype(np.float32) / 32768.0
        ms = float(np.mean(x * x))
        if ms > pcm.SILENCE_FLOOR ** 2:              # quiet moments don't pull the level up
            if self.mean_sq is None:
                self.mean_sq = ms
                self.gain = self._want()
            else:
                keep = math.exp(-n / (self.tau_s * pcm.SAMPLE_RATE))
                self.mean_sq = ms + (self.mean_sq - ms) * keep
        if self.mean_sq is not None:
            self.gain += (self._want() - self.gain) * min(1.0, n / (2.0 * pcm.SAMPLE_RATE))
        return pcm.apply_gain(block, self.gain)

    def _want(self) -> float:
        return float(np.clip(self.target / math.sqrt(self.mean_sq), self.lo, self.hi))


# --- one station playing -------------------------------------------------------------------

def open_stream(url: str):
    """What plays a station: a receiver's own client if the address is one's
    (OpenWebRX, KiwiSDR: see receiver.py), a room's (room.py), else the ordinary stream."""
    from sleepradiopi.playback import receiver, room
    if room.is_room(url):
        return room.RoomStream(url)
    return receiver.ReceiverStream(url) if receiver.is_receiver(url) else RadioStream(url)


class RadioStream:
    """One connection to a station. A fetch thread sends the stream's bytes to
    ffmpeg; a decode thread queues ffmpeg's PCM for the show, which takes it
    with read(). ended says why, once either side has stopped; close() stops both."""

    def __init__(self, url: str, fetch: Callable = _get, decoder: list[str] | None = None,
                 headers: dict | None = None) -> None:
        self.url = url
        self.headers = headers or {}           # e.g. a Range, to start part-way through (podcasts)
        self.total_bytes = 0                   # from the server's Content-Range, when it sends one
        self._fetch = fetch
        self._cmd = decoder or DECODER
        self.title: str | None = None          # ICY now-playing, if the station sends it
        self.ended: str | None = None          # why the stream stopped (None while it plays)
        self._closed = threading.Event()
        self._blocks: queue.Queue = queue.Queue(QUEUE_BLOCKS)
        self._proc: subprocess.Popen | None = None
        self._resp = None

    def start(self) -> None:
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        threading.Thread(target=self._fetch_run, name="radio-fetch", daemon=True).start()
        threading.Thread(target=self._decode_run, name="radio-decode", daemon=True).start()

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        """The next block of PCM, or None if none came within timeout."""
        try:
            return self._blocks.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def buffered_s(self) -> float:
        return self._blocks.qsize() * pcm.CHUNK_FRAMES / pcm.SAMPLE_RATE

    def close(self) -> None:
        self._closed.set()
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        proc = self._proc
        if proc is not None:
            proc.kill()
            proc.wait()
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass

    def _end(self, why: str) -> None:
        if self.ended is None and not self._closed.is_set():
            self.ended = why
            log.info("radio: stream ended: %s", why)

    # --- fetching (thread) ---------------------------------------------------------------

    def _fetch_run(self) -> None:
        try:
            self._follow(self.url)
        except StreamError as e:
            self._end(str(e))
        except Exception as e:                         # incl. a read cut short by close()
            if not self._closed.is_set():
                self._end(_reason(e) if isinstance(e, OSError) else f"the stream broke ({e})")
        finally:
            try:
                self._proc.stdin.close()               # ffmpeg plays what it has, then ends
            except Exception:
                pass

    def _open(self, url: str):
        try:
            resp = self._fetch(url, headers={"Icy-MetaData": "1", **self.headers})
            m = re.match(r"bytes \d+-\d+/(\d+)", resp.headers.get("Content-Range") or "") if hasattr(resp, "headers") else None
            if m:
                self.total_bytes = int(m.group(1))
            return resp
        except urllib.error.HTTPError as e:
            raise StreamError(f"the station said {e.code} {e.reason}") from None

    def _follow(self, url: str) -> None:
        for _ in range(MAX_HOPS):
            resp = self._resp = self._open(url)
            base = resp.geturl() if hasattr(resp, "geturl") else url
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            path = urlparse(base).path.lower()
            listy = (path.endswith((".m3u", ".m3u8", ".pls")) or "mpegurl" in ctype or "scpls" in ctype
                     or ctype in ("text/plain", "application/pls+xml"))
            if not listy:
                self._pump(resp)
                return
            text = resp.read(PLAYLIST_BYTES).decode("utf-8", errors="replace")
            resp.close()
            if is_hls(text):
                self._hls(base, text)
                return
            urls = playlist_urls(text, base)
            if not urls:
                raise StreamError("the station's playlist has no stream in it")
            url = urls[0]
        raise StreamError("too many playlists inside playlists")

    def _write(self, data: bytes) -> None:
        if self._closed.is_set():
            raise StreamError("closed")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()               # a slow station mustn't sit in the pipe's buffer
        except (BrokenPipeError, ValueError, OSError):
            raise StreamError("the station's audio couldn't be decoded") from None

    def _pump(self, resp) -> None:
        """A plain (Icecast/Shoutcast) stream, taking the ICY titles out of it."""
        try:
            metaint = int(resp.headers.get("icy-metaint") or 0)
        except ValueError:
            metaint = 0
        if not metaint:
            while data := resp.read(16_384):
                self._write(data)
            return
        while True:
            audio = _read_exactly(resp, metaint)
            if audio is None:
                return
            self._write(audio)
            size = resp.read(1)
            if not size:
                return
            if size[0]:
                meta = _read_exactly(resp, size[0] * 16)
                if meta is None:
                    return
                title = icy_title(meta)
                if title is not None and (title or None) != self.title:
                    self.title = title or None
                    log.info("radio: now playing %s", title or "(no title)")

    def _hls(self, url: str, text: str) -> None:
        variant = hls_variant(text, url)
        if variant is not None:
            url, text = variant, self._text(variant)
        seen = None
        sent_map = None
        misses = 0                                       # fetches lost in a row
        while not self._closed.is_set():
            pl = hls_media(text, url)
            if pl["encrypted"]:
                raise StreamError("this station's stream is encrypted (not supported)")
            if pl["map"] and pl["map"] != sent_map:
                self._write(self._bytes(pl["map"]))
                sent_map = pl["map"]
            if seen is None:                             # join near the live edge
                seen = pl["seq"] + max(0, len(pl["segments"]) - HLS_LIVE_EDGE) - 1
            new = 0
            for i, seg in enumerate(pl["segments"]):
                n = pl["seq"] + i
                if n <= seen:
                    continue
                try:
                    data = self._retried(self._bytes, seg)
                except (StreamError, OSError, http.client.HTTPException) as e:
                    misses += 1                          # a blip beats a reconnect: skip it
                    if misses >= HLS_MAX_MISSES:
                        raise
                    log.info("radio: skipped a segment (%s)", e)
                else:
                    misses = 0
                    self._write(data)
                seen = n
                new += 1
            if pl["ended"]:
                return
            if self._closed.wait(pl["target"] / 2 if new else pl["target"] / 3):
                return
            try:
                text = self._retried(self._text, url)
            except (StreamError, OSError, http.client.HTTPException):
                misses += 1                              # try again next time round
                if misses >= HLS_MAX_MISSES:
                    raise

    def _retried(self, fetch: Callable, url: str):
        """fetch(url), trying again when the connection drops (not on an HTTP
        error: a 404'd segment won't come back)."""
        for attempt in range(HLS_TRIES):
            try:
                return fetch(url)
            except (OSError, http.client.HTTPException):
                if attempt == HLS_TRIES - 1 or self._closed.wait(HLS_RETRY_S * (attempt + 1)):
                    raise

    def _bytes(self, url: str) -> bytes:
        resp = self._resp = self._open(url)
        with resp:
            return resp.read()

    def _text(self, url: str) -> str:
        with self._open(url) as resp:
            return resp.read(PLAYLIST_BYTES).decode("utf-8", errors="replace")

    # --- decoding (thread) ----------------------------------------------------------------

    def _decode_run(self) -> None:
        proc = self._proc
        chunk = pcm.CHUNK_FRAMES * pcm.BYTES_PER_FRAME
        pending = b""
        got_any = False
        try:
            while data := proc.stdout.read(chunk):
                pending += data
                usable = len(pending) - len(pending) % pcm.BYTES_PER_FRAME
                if not usable:
                    continue
                block = np.frombuffer(pending[:usable], dtype=np.int16).reshape(-1, pcm.CHANNELS)
                pending = pending[usable:]
                got_any = True
                while not self._closed.is_set():
                    try:
                        self._blocks.put(block, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if self._closed.is_set():
                    return
        except (OSError, ValueError):
            return
        if self._closed.is_set():
            return
        try:
            err = proc.stderr.read(2000)
        except (OSError, ValueError):
            err = b""
        if err and self.ended is None:           # (not after the fetch failed: that says why)
            log.warning("radio: ffmpeg: %s", err.decode(errors="replace").strip()[-300:])
        # The fetch side usually knows better why it stopped; wait briefly for it.
        proc.wait()
        self._end("the station stopped sending" if got_any else "the station's audio couldn't be played")


def _read_exactly(resp, n: int) -> bytes | None:
    parts, left = [], n
    while left:
        data = resp.read(left)
        if not data:
            return None
        parts.append(data)
        left -= len(data)
    return b"".join(parts)


def _reason(e: Exception) -> str:
    text = str(getattr(e, "reason", e))
    if "Name or service not known" in text or "resolution" in text or "nodename" in text:
        return "couldn't find the station (is the radio online?)"
    if isinstance(e, TimeoutError) or "timed out" in text:
        return "the station isn't answering"
    return f"couldn't reach the station ({text})"
