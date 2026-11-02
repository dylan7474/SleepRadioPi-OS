"""Ebooks the radio reads aloud: epub, plain text (Project Gutenberg's) and html files in the Ebooks folder.

This module turns a file into what the reader speaks: chapters, each a list of paragraphs, and the whole
book as a flat list of SPEECH UNITS -- one sentence each, with the pause to leave after it. It never
touches audio; playback/reader.py speaks the units and the station plays them.

Position is kept in estimated MILLISECONDS (the characters before it x MS_PER_CHAR), so the radio's existing
places, seek (-/+ a minute) and progress bar work unchanged on an ebook; a unit's start is where the
estimate puts it, and nothing needs the real length of speech, which isn't known until it's made.

What the text goes through before it's spoken (the voice worker splits at every full stop, so a stop that
isn't the end of a sentence would make it pause mid-name):
  * "Mr. Bennet" -> "Mister Bennet" (and Mrs, Dr, St, Capt ...: abbreviations are spoken in full), and an
    initial ("J. R. Smith") loses its stop;
  * sentences are found with those protected, and long ones are left whole (the worker cuts them at commas);
  * footnote marks ([1], [Footnote 3]), page numbers and bare rules are dropped.
"""

from __future__ import annotations

import bisect
import html
import json
import logging
import re
import threading
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from posixpath import dirname, join as pjoin, normpath
from xml.etree import ElementTree as ET

from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

EBOOK_FILES = {".epub", ".txt", ".html", ".htm"}
MIN_MEMORY_KB = 800_000          # a Pi 3 (~950 MB) or better can read aloud; a Zero 2 W (~470 MB) cannot: the voice (150-260 MB)
                                 # beside the station ran it out of memory (2026-11-02)


def memory_kb() -> int:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def enabled(setting="auto", memory: int | None = None) -> bool:
    """Whether this radio reads ebooks: "on" / "off", or "auto" (the default) = when it has the memory for it."""
    if setting in ("on", True):
        return True
    if setting in ("off", False):
        return False
    return (memory_kb() if memory is None else memory) >= MIN_MEMORY_KB


EBOOK = "@ebook/"                # the keys of ebooks, in the places an audiobook's key goes (positions, sources, presets)
MS_PER_CHAR = 67                 # ~15 characters a second: the estimate behind positions (the voices run 13-16)
MAX_BYTES = 40_000_000           # an epub or text bigger than this isn't read
MAX_CHARS = 6_000_000            # a book longer than this is cut (the Bible is ~4.5 million)
SENTENCE_PAUSE_S = 0.35
PARAGRAPH_PAUSE_S = 0.9
CHAPTER_PAUSE_S = 1.6

ABBREV = {"mr": "Mister", "mrs": "Missus", "dr": "Doctor", "st": "Saint", "prof": "Professor", "capt": "Captain",
          "col": "Colonel", "lt": "Lieutenant", "gen": "General", "rev": "Reverend", "hon": "Honourable",
          "sgt": "Sergeant", "messrs": "Messrs", "mme": "Madame", "mlle": "Mademoiselle", "ms": "Ms",
          "mt": "Mount", "sr": "Senior", "jr": "Junior"}
_ABBR_RE = re.compile(r"\b(" + "|".join(sorted(ABBREV, key=len, reverse=True)) + r")\.(?=\s+[A-Z\"'“‘(]|\s*$)", re.I)
_INITIAL_RE = re.compile(r"\b([A-Z])\.(?=\s+[A-Z][a-z]|\s+[A-Z]\.)")
_FOOTNOTE_RE = re.compile(r"\[(?:Footnote\s*)?\d{1,3}\]|\[[A-Za-z]\]|\{\d+\}")
_END_RE = re.compile(r"([.!?]+[\"'”’)\]]*)\s+")


class EbookError(ValueError):
    """Couldn't read an ebook: the text is for the page."""


# --- the book -----------------------------------------------------------------------------

@dataclass
class Chapter:
    title: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class Unit:
    """One thing to say: a sentence (or a chapter's heading), and the pause after it."""
    chapter: int
    text: str
    pause_s: float
    start_ms: int = 0             # where the estimate puts it in the book


@dataclass
class Ebook:
    key: str
    title: str
    author: str
    chapters: list[Chapter]
    units: list[Unit] = field(default_factory=list)
    total_ms: int = 0

    def __post_init__(self) -> None:
        self._starts: list[int] = []
        self._build()

    def _build(self) -> None:
        at = 0
        self.units = []
        for ci, ch in enumerate(self.chapters):
            heading = ch.title.strip()
            if heading:
                said = speakable(heading)
                self.units.append(Unit(ci, said if said.endswith((".", "!", "?")) else said + ".", CHAPTER_PAUSE_S, int(at * MS_PER_CHAR)))
                at += len(heading) + 1
            for p in ch.paragraphs:
                sents = sentences(p)
                for si, s in enumerate(sents):
                    last = si == len(sents) - 1
                    self.units.append(Unit(ci, s, PARAGRAPH_PAUSE_S if last else SENTENCE_PAUSE_S, int(at * MS_PER_CHAR)))
                    at += len(s) + 1
        self.total_ms = int(at * MS_PER_CHAR)
        self._starts = [u.start_ms for u in self.units]

    def unit_at(self, pos_ms: int) -> int:
        """The index of the unit that the estimated position is in (the one starting at or before it)."""
        if not self.units:
            return 0
        return max(0, min(len(self.units) - 1, bisect.bisect_right(self._starts, max(0, pos_ms)) - 1))

    def chapter_starts(self) -> list[int]:
        """Each chapter's estimated start (ms)."""
        out, seen = [], set()
        for u in self.units:
            if u.chapter not in seen:
                seen.add(u.chapter)
                out.append(u.start_ms)
        return out


@dataclass
class ChapterView:
    title: str
    offset_ms: int
    length_ms: int


class EbookView:
    """An ebook as the station sees any book: a key, a title, chapters with offsets, a length (estimated) and at(pos).
    The key carries the EBOOK prefix; the ebook itself is .ebook."""

    def __init__(self, ebook: Ebook) -> None:
        self.ebook = ebook
        self.key = EBOOK + ebook.key
        self.title, self.author = ebook.title, ebook.author
        self.total_ms = ebook.total_ms
        firsts: dict[int, int] = {}
        for u in ebook.units:
            firsts.setdefault(u.chapter, u.start_ms)
        order = sorted(firsts.items())
        self.chapters = []
        for n, (ci, start) in enumerate(order):
            end = order[n + 1][1] if n + 1 < len(order) else ebook.total_ms
            self.chapters.append(ChapterView(ebook.chapters[ci].title or f"Chapter {n + 1}", start, max(1, end - start)))

    def at(self, pos_ms: int) -> tuple[int, int]:
        pos_ms = max(0, pos_ms)
        for i, c in enumerate(self.chapters):
            if pos_ms < c.offset_ms + c.length_ms:
                return i, pos_ms - c.offset_ms
        return len(self.chapters) - 1, self.chapters[-1].length_ms if self.chapters else 0


# --- text -----------------------------------------------------------------------------------

def speakable(text: str) -> str:
    """What to give the voice for a stretch of text: abbreviations in full, no stops that aren't sentence ends,
    no footnote marks, one space between words."""
    text = text.replace("_", "")                                  # (Gutenberg's italics: _Mr. Darcy_)
    text = _FOOTNOTE_RE.sub("", text)
    text = _ABBR_RE.sub(lambda m: ABBREV[m.group(1).lower()], text)
    text = _INITIAL_RE.sub(r"\1", text)
    text = text.replace("&", " and ").replace("_", "")
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"[‘’]", "'", text)
    text = re.sub(r"\s*[—–]+\s*|\s+--+\s+", ", ", text)         # a dash is a short pause; the voice says "dash" at times
    text = re.sub(r"\s*\*\s*(\*\s*)+", " ", text)                # ornament lines
    return " ".join(text.split())


def sentences(paragraph: str) -> list[str]:
    """A paragraph as sentences, each ready to speak (the abbreviations already in full)."""
    text = speakable(paragraph)
    if not text:
        return []
    out, start = [], 0
    for m in _END_RE.finditer(text):
        out.append(text[start:m.end(1)].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if re.search(r"[A-Za-z0-9]", s)]


# --- reading files -------------------------------------------------------------------------

class _Text(HTMLParser):
    """HTML -> paragraphs, with a heading noted as such."""
    BLOCK = {"p", "div", "br", "li", "tr", "blockquote", "pre", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt"}
    SKIP = {"script", "style", "head", "title", "svg", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[tuple[str, str]] = []        # (kind "h"|"p", text)
        self._buf: list[str] = []
        self._skip = 0
        self._heading = False

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.parts.append(("h" if self._heading else "p", text))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._flush()
            self._heading = tag in ("h1", "h2", "h3", "h4")
        elif tag == "sup":
            self._skip += 1                            # footnote marks

    def handle_endtag(self, tag):
        if tag in self.SKIP or tag == "sup":
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self._flush()
            self._heading = False

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _chapters_from_parts(parts: list[tuple[str, str]], default_title: str) -> list[Chapter]:
    """Headings start chapters; a heading with nothing under it joins the next one's title."""
    chapters: list[Chapter] = []
    pending: list[str] = []
    for kind, text in parts:
        if kind == "h":
            if chapters and not chapters[-1].paragraphs and not pending:
                chapters[-1].title = (chapters[-1].title + ". " + text).strip(". ")
            else:
                pending.append(text)
            continue
        if pending or not chapters:
            chapters.append(Chapter(". ".join(pending) or default_title))
            pending = []
        chapters[-1].paragraphs.append(text)
    if pending and not chapters:
        chapters.append(Chapter(". ".join(pending)))
    return [c for c in chapters if c.paragraphs]


def parse_html(data: bytes | str, default_title: str = "") -> list[Chapter]:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    p = _Text()
    p.feed(text)
    p.close()
    return _chapters_from_parts(p.parts, default_title)


_GB_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
_GB_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
_HEADING_LINE = re.compile(r"^\s*((?:CHAPTER|Chapter|BOOK|Book|PART|Part|VOLUME|Volume|LETTER|Letter|ACT|Act|SCENE|Scene|STAVE|Stave)\s+[IVXLCDM\d]+[.:]?(?:\s+.{0,60})?|[IVXLCDM]{1,7}\.)\s*$")


def parse_text(data: bytes | str) -> tuple[list[Chapter], dict]:
    """Plain text: paragraphs are blank-line separated, lines inside one are joined. Project Gutenberg's
    header and licence are dropped; lines like 'CHAPTER IV.' start chapters. Returns (chapters, {title, author})."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    meta = {}
    for key in ("Title", "Author"):
        m = re.search(rf"^{key}:\s*(.+)$", text[:6000], re.M)
        if m:
            meta[key.lower()] = m.group(1).strip()
    start = _GB_START.search(text)
    if start:
        text = text[start.end():]
    end = _GB_END.search(text)
    if end:
        text = text[:end.start()]
    def caption(m):                                              # (a picture's caption, over several lines; some editions
        found = re.search(r"\b(?:Chapter|CHAPTER)\s+[IVXLCDM\d]+\.?", m.group(0))     # hide a chapter's heading in one)
        return f"\n\n{found.group(0)}\n\n" if found else ""
    text = re.sub(r"\[Illustration[^\]]*\]", caption, text, flags=re.I)
    parts: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if len(lines) <= 3 and all(_HEADING_LINE.match(ln) for ln in lines[:1]) and len(" ".join(lines)) < 90:
            parts.append(("h", " ".join(lines)))
            continue
        parts.append(("p", " ".join(lines)))
    return _chapters_from_parts(_without_title_page(parts), meta.get("title", "")), meta


def _without_title_page(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop the title-page lines before the first real paragraph (a long one: a preface stays)."""
    for i, (kind, text) in enumerate(parts):
        if kind == "p" and len(text) >= 150:
            if i >= 40:
                return parts
            return parts[i - 1:] if i and parts[i - 1][0] == "h" else parts[i:]       # (its heading stays)
    return parts


def _opf_path(z: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        raise EbookError("that isn't an epub the radio can read") from None
    for el in root.iter():
        if el.tag.endswith("rootfile") and el.get("full-path"):
            return el.get("full-path")
    raise EbookError("that epub has no book in it")


def parse_epub(path: Path) -> tuple[list[Chapter], dict]:
    """An epub: the spine's pages in order, each read as html. Returns (chapters, {title, author})."""
    if path.stat().st_size > MAX_BYTES:
        raise EbookError(f"{path.name}: too big to read")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise EbookError(f"{path.name}: not a valid epub") from None
    with z:
        opf = _opf_path(z)
        try:
            root = ET.fromstring(z.read(opf))
        except (KeyError, ET.ParseError):
            raise EbookError(f"{path.name}: its book file is damaged") from None
        ns = lambda el: el.tag.rsplit("}", 1)[-1]                      # noqa: E731 (namespaces vary)
        meta = {}
        manifest, spine = {}, []
        for el in root.iter():
            name = ns(el)
            if name == "title" and "title" not in meta and (el.text or "").strip():
                meta["title"] = el.text.strip()
            elif name == "creator" and "author" not in meta and (el.text or "").strip():
                meta["author"] = el.text.strip()
            elif name == "item" and el.get("id"):
                manifest[el.get("id")] = (el.get("href", ""), el.get("media-type", ""))
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
        base = dirname(opf)
        chapters: list[Chapter] = []
        total = 0
        for idref in spine:
            href, mtype = manifest.get(idref, ("", ""))
            if not href or "html" not in mtype and not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            name = normpath(pjoin(base, href.split("#")[0]))
            try:
                page = z.read(name)
            except KeyError:
                continue
            chapters += parse_html(page, "")
            total += len(page)
            if total > MAX_BYTES:
                break
    # pages without a heading of their own: carry on the previous chapter's flow
    merged: list[Chapter] = []
    for ch in chapters:
        if not ch.title and merged:
            merged[-1].paragraphs += ch.paragraphs
        else:
            merged.append(ch)
    return merged, meta


def read_ebook(path: Path, key: str | None = None) -> Ebook:
    """Parse a file into an Ebook (EbookError, with words for the page, if it can't be read)."""
    ext = path.suffix.lower()
    if ext not in EBOOK_FILES:
        raise EbookError(f"{path.name}: the radio reads {', '.join(sorted(EBOOK_FILES))} files")
    try:
        if ext == ".epub":
            chapters, meta = parse_epub(path)
        elif path.stat().st_size > MAX_BYTES:
            raise EbookError(f"{path.name}: too big to read")
        elif ext == ".txt":
            chapters, meta = parse_text(path.read_bytes())
        else:
            chapters, meta = parse_html(path.read_bytes(), path.stem), {}
    except OSError as e:
        raise EbookError(f"{path.name}: couldn't read it ({e.strerror})") from None
    chars = 0
    for ch in chapters:
        keep = []
        for p in ch.paragraphs:
            chars += len(p)
            if chars > MAX_CHARS:
                break
            keep.append(p)
        ch.paragraphs = keep
    chapters = [c for c in chapters if c.paragraphs]
    if not chapters:
        raise EbookError(f"{path.name}: no text in it to read")
    title = meta.get("title") or path.stem.replace("_", " ")
    return Ebook(key or path.name, title[:200], (meta.get("author") or "")[:200], chapters)


# --- the folder ------------------------------------------------------------------------------

@dataclass
class EbookInfo:
    key: str
    title: str
    author: str
    chapters: int
    total_ms: int
    error: str = ""


class EbookLibrary:
    """The Ebooks folder: what's in it (listed with titles, kept in a small cache so a scan isn't a re-read)
    and any one of them parsed on demand. A file that can't be read is listed with what's wrong."""

    def __init__(self, root: Path, cache_path: Path | None = None) -> None:
        self.root = Path(root)
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._lock = threading.Lock()
        self._info: dict[str, EbookInfo] = {}
        self._open: dict[str, Ebook] = {}               # the one being read (and the last)
        self._cache = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text()) if self.cache_path else {}
        except (OSError, ValueError):
            return {}

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted((p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in EBOOK_FILES
                       and not any(part.startswith(".") for part in p.relative_to(self.root).parts)),
                      key=lambda p: str(p).lower())

    def scan(self) -> list[EbookInfo]:
        found: dict[str, EbookInfo] = {}
        cache: dict = {}
        for p in self._files():
            key = str(p.relative_to(self.root))
            st = p.stat()
            sig = f"{st.st_size}|{int(st.st_mtime)}"
            hit = self._cache.get(key)
            if hit and hit.get("sig") == sig:
                info = EbookInfo(key, hit["title"], hit["author"], hit["chapters"], hit["total_ms"], hit.get("error", ""))
            else:
                try:
                    book = read_ebook(p, key)
                    info = EbookInfo(key, book.title, book.author, len(book.chapters), book.total_ms)
                except EbookError as e:
                    info = EbookInfo(key, p.stem, "", 0, 0, str(e))
            cache[key] = {"sig": sig, "title": info.title, "author": info.author, "chapters": info.chapters,
                          "total_ms": info.total_ms, "error": info.error}
            found[key] = info
        with self._lock:
            self._info = found
            self._cache = cache
            self._open = {k: v for k, v in self._open.items() if k in found}
        if self.cache_path is not None and cache != self._load():
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(self.cache_path, json.dumps(cache))
            except OSError:
                pass
        return list(found.values())

    def all(self) -> list[EbookInfo]:
        with self._lock:
            return list(self._info.values())

    def get(self, key: str) -> Ebook | None:
        """The parsed book (kept while it's being read), or None if there's no such file or it can't be read."""
        with self._lock:
            if key in self._open:
                return self._open[key]
        path = (self.root / key)
        try:
            path.resolve().relative_to(self.root.resolve())
            book = read_ebook(path, key)
        except (ValueError, EbookError, OSError):
            return None
        with self._lock:
            self._open = {key: book}                    # one at a time: a long book is a few MB of text
        return book
