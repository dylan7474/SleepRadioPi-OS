"""LibriVox: free public-domain audiobooks, read by volunteers.

The catalogue is LibriVox's own JSON feed (no key); the audio is a plain MP3
per chapter on archive.org. Like podcasts, the station fetches the audio in
Python (playback/radio.py's RadioStream, with HTTP Range to resume part-way
through a chapter -- archive.org answers 206 through its redirects).

What the live feed does (checked 2026-11-01), and what this module does about it:
  * title= is an EXACT match; "^text" makes it a prefix match -> search sends "^".
  * genre= is unreliable (some genres answer 500) -> not offered.
  * a book not found is {"error": ...} with a 404, not an empty list -> [].
  * descriptions carry HTML (<em>, <p>) -> stripped.
  * every number but totaltimesecs is a string; a request now and then
    fails outright -> one retry.
  * the cover is not in the payload: archive.org/services/img/<identifier>.

A followed book is a podcast to the rest of the radio: feed() turns it into the
shape podcasts.parse_feed gives (a "show" whose address is librivox:<id>, its
chapters the episodes, chapter 1 the oldest), so following, the chapter list
cached on the radio, each chapter's remembered place, preset buttons and
playing on from chapter to chapter are all podcasts.py's. The desktop shows
these in Audiobooks, oldest chapter first (a book is read in order).
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

FEED = "https://librivox.org/api/feed/audiobooks/"
COVER = "https://archive.org/services/img/"
MAX_BYTES = 8_000_000
PAGE = 20
SEARCH_TTL_S = 15 * 60         # a repeated search inside this is answered from memory
SEARCH_KEEP = 40
DONE_MARGIN_MS = 60_000        # within a minute of the end counts as heard


class LibriVoxError(ValueError):
    """Couldn't find or read a book: the text is for the page."""


def _get(url: str, limit: int, headers: dict | None = None, timeout: float = 20) -> bytes:
    """One request, tried a second time if it fails outright (the feed does, now and then).
    A 404 is an answer (LibriVox's way of saying 'nothing found'), so it is returned."""
    last: Exception | None = None
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={"User-Agent": radio_mod.USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(limit)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return e.read(limit)
            last = e
        except OSError as e:
            last = e
        if attempt == 1:
            time.sleep(1)
    raise last                                          # type: ignore[misc]


def clean_text(raw) -> str:
    """HTML out of a description: tags dropped, entities decoded, blanks squashed."""
    text = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", " ", str(raw or ""), flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    return " ".join(html.unescape(text).split())


def https(url) -> str:
    """The address with https (archive.org and LibriVox both serve it); '' if it isn't a web address."""
    url = str(url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return "https://" + url.split("://", 1)[1]


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _identifier(url_iarchive) -> str:
    """'https://www.archive.org/details/pride_and_prejudice_librivox' -> 'pride_and_prejudice_librivox'."""
    path = urlparse(str(url_iarchive or "")).path.strip("/")
    return path.split("/", 1)[1].split("/")[0] if path.startswith("details/") else ""


def _authors(book: dict) -> str:
    names = []
    for a in book.get("authors") or []:
        if isinstance(a, dict):
            name = " ".join(x for x in (str(a.get("first_name") or "").strip(), str(a.get("last_name") or "").strip()) if x)
            if name:
                names.append(name)
    return ", ".join(names)


def parse_book(raw: dict) -> dict:
    """One feed entry -> {id, title, author, about, minutes, chapters, cover, language, year, url}."""
    ident = _identifier(raw.get("url_iarchive"))
    return {
        "id": str(raw.get("id") or ""),
        "title": clean_text(raw.get("title"))[:200],
        "author": _authors(raw)[:200],
        "about": clean_text(raw.get("description"))[:1500],
        "duration_ms": _int(raw.get("totaltimesecs")) * 1000,
        "sections": _int(raw.get("num_sections")),
        "cover": COVER + quote(ident) if ident else "",
        "language": clean_text(raw.get("language"))[:40],
        "year": clean_text(raw.get("copyright_year"))[:10],
        "url": https(raw.get("url_librivox")),
    }


def parse_search(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    return [b for b in (parse_book(r) for r in data.get("books") or [] if isinstance(r, dict)) if b["id"] and b["title"]]


def parse_chapters(raw: dict) -> list[dict]:
    """The sections of an extended entry -> [{id, number, title, url, duration_ms, reader}] in order.
    A section with no usable audio address is left out."""
    out = []
    for s in raw.get("sections") or []:
        if not isinstance(s, dict):
            continue
        url = https(s.get("listen_url"))
        cid = str(s.get("id") or "")
        if not url or not cid:
            continue
        readers = [clean_text(r.get("display_name")) for r in s.get("readers") or [] if isinstance(r, dict)]
        out.append({
            "id": cid,
            "number": _int(s.get("section_number"), len(out) + 1),
            "title": clean_text(s.get("title"))[:200] or f"Chapter {len(out) + 1}",
            "url": url,
            "duration_ms": _int(s.get("playtime")) * 1000,
            "reader": ", ".join(r for r in readers if r)[:120],
        })
    out.sort(key=lambda c: c["number"])
    return out


SCHEME = "librivox:"


def is_book(feed_url: str) -> bool:
    return str(feed_url).startswith(SCHEME)


def book_id(feed_url: str) -> str:
    bid = str(feed_url)[len(SCHEME):].strip()
    if not bid.isdigit():
        raise LibriVoxError("that isn't a LibriVox book")
    return bid


def search(query: str, kind: str = "title", offset: int = 0, fetch=_get) -> list[dict]:
    """Books whose title starts with the text, or whose author's surname is the last word
    of it. PAGE at a time (offset steps through them). Each has feed_url, to follow it."""
    q = query.strip()
    if not q:
        return []
    if kind not in ("title", "author"):
        raise LibriVoxError("search by title or by author")
    # author= matches the surname (checked live); title= needs the "^" for a prefix.
    term = ("^" + q) if kind == "title" else q.split()[-1]
    url = f"{FEED}?{kind}={quote(term)}&format=json&limit={PAGE}&offset={max(0, int(offset))}"
    try:
        data = json.loads(fetch(url, MAX_BYTES, {"Accept": "application/json"}))
    except (OSError, ValueError) as e:
        log.warning("librivox search %r failed: %s", q, e)
        raise LibriVoxError("couldn't reach LibriVox (is the radio online?)") from None
    books = parse_search(data)
    for b in books:
        b["feed_url"] = SCHEME + b["id"]
    return books


def feed(book_id_: str, fetch=_get) -> dict:
    """A book as podcasts.py keeps a show: {"title", "author", "serial": True, "cover", "about",
    "episodes": [{guid, title, url, type, bytes, pub, duration_ms, about}]}. Newest first, which for a
    book is the LAST chapter first (pub is the chapter's number), as for any show."""
    try:
        data = json.loads(fetch(f"{FEED}?id={book_id_}&extended=1&format=json", MAX_BYTES, {"Accept": "application/json"}))
    except (OSError, ValueError) as e:
        raise LibriVoxError("couldn't reach LibriVox (is the radio online?)") from None
    raws = data.get("books") if isinstance(data, dict) else None
    if not raws or not isinstance(raws[0], dict):
        raise LibriVoxError("LibriVox has no such book")
    book = parse_book(raws[0])
    chapters = parse_chapters(raws[0])
    if not chapters:
        raise LibriVoxError("that book has no chapters to play")
    eps = [{"guid": c["id"], "title": c["title"], "url": c["url"], "type": "audio/mpeg", "bytes": 0,
            "pub": float(c["number"]), "duration_ms": c["duration_ms"],
            "about": f"Read by {c['reader']}" if c["reader"] else ""} for c in chapters]
    eps.sort(key=lambda e: -e["pub"])
    return {"title": book["title"], "author": book["author"], "serial": True, "cover": book["cover"],
            "about": book["about"], "episodes": eps}
