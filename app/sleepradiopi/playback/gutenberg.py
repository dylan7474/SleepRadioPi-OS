"""Project Gutenberg: free public-domain ebooks, found through its own catalogue search.

The catalogue answers an OPDS (Atom) feed, no key: /ebooks/search.opds/?query=<words>&start_index=<n>, 25 a page
(checked 2026-11-02). Each entry is a book -- <id>.../ebooks/<number>.opds, its title, and the author in <content>
(a book with no author has "1239 downloads" there instead: dropped). Other entries (a next-page link, a category)
don't match /ebooks/<number>.opds and are skipped. The books themselves are plain files:
/ebooks/<number>.txt.utf-8 (what the radio reads best: it is what the Ebooks folder has been tested with) or,
where there is no text, /ebooks/<number>.epub.noimages.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from sleepradiopi.playback import librivox as lv_mod

SEARCH = "https://www.gutenberg.org/ebooks/search.opds/"
FILES = ((".txt", "https://www.gutenberg.org/ebooks/{id}.txt.utf-8"),
         (".epub", "https://www.gutenberg.org/ebooks/{id}.epub.noimages"))
PAGE = 25
MAX_BYTES = 4_000_000          # (one results page is ~25 KB with thumbnails inline; plenty)
ATOM = "{http://www.w3.org/2005/Atom}"


class GutenbergError(ValueError):
    """Couldn't search: the text is for the page."""


def parse_search(raw: bytes) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise GutenbergError("Project Gutenberg's answer wasn't readable") from None
    out = []
    for e in root.findall(ATOM + "entry"):
        m = re.search(r"/ebooks/(\d+)\.opds\s*$", e.findtext(ATOM + "id") or "")
        title = " ".join((e.findtext(ATOM + "title") or "").split())
        if not m or not title:
            continue
        author = " ".join((e.findtext(ATOM + "content") or "").split())
        if re.fullmatch(r"[\d,]+ downloads?", author):
            author = ""
        out.append({"id": m.group(1), "title": title[:200], "author": author[:120]})
    return out


def search(query: str, offset: int = 0, fetch=lv_mod._get) -> list[dict]:
    """Books matching the words (title or author), PAGE at a time (offset steps through them)."""
    q = " ".join(query.split())
    if not q:
        return []
    url = f"{SEARCH}?query={quote_plus(q)}" + (f"&start_index={int(offset) + 1}" if int(offset) > 0 else "")
    try:
        raw = fetch(url, MAX_BYTES, {"Accept": "application/atom+xml"})
    except OSError as e:
        raise GutenbergError("couldn't reach Project Gutenberg (is the radio online?)") from None
    return parse_search(raw)


def book_id(text) -> str:
    text = str(text or "").strip()
    if not text.isdigit():
        raise GutenbergError("that isn't a Project Gutenberg book")
    return text
