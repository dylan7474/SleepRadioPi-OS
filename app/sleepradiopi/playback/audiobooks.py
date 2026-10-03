"""Audiobooks: find the books, their chapters, and where each one was left.

A book is either a folder of mp3 or m4a files (one per chapter, in natural
file order; the whole book is everything under the folder, e.g. CD1/, CD2/)
or a single .m4b, .m4a or .mp3 file (an .m4b's or .m4a's own chapter markers
become its chapters).
Books live in their own folder next to the music: Author/Book/..., or Book/...

Every book remembers its place -- one position in ms through the whole book
-- saved in a small JSON file on the radio's writable storage, so it
survives pauses, the sleep timer, switching to something else, restarts and
power cuts. The station (broadcast/station.py) plays a book as a "source",
with no DJ, and seeks by restarting the decode at the new point.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

BOOK_FILES = {".mp3", ".m4b", ".m4a"}
MARKED = {".m4b", ".m4a"}  # these can carry chapter markers of their own
KEPT = "@ondemand/"        # the keys of On demand things that remember their place (KeptLibrary), beside the books' own
KEPT_FILES = BOOK_FILES | {".flac", ".m4a", ".ogg", ".opus", ".wav"}
CACHE_VERSION = 1


def _natural(name: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


@dataclass
class Chapter:
    path: Path
    start_ms: int          # in its file (an m4b's chapters share one file)
    end_ms: int
    title: str
    offset_ms: int = 0     # where it starts in the whole book

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class Book:
    key: str               # its path under the audiobooks folder: the id everything uses
    title: str
    author: str
    chapters: list[Chapter] = field(default_factory=list)

    @property
    def total_ms(self) -> int:
        return sum(c.length_ms for c in self.chapters)

    def at(self, pos_ms: int) -> tuple[int, int]:
        """(chapter index, ms into that chapter) for a place in the whole book."""
        pos_ms = max(0, pos_ms)
        for i, c in enumerate(self.chapters):
            if pos_ms < c.offset_ms + c.length_ms:
                return i, pos_ms - c.offset_ms
        return len(self.chapters) - 1, self.chapters[-1].length_ms if self.chapters else 0


def _tag(tags: dict, *names) -> str:
    for n in names:
        v = tags.get(n)
        if v:
            return (v[0] if isinstance(v, list) else str(v)).strip()
    return ""


def _probe(path: Path) -> list:
    """[duration ms, [[start ms, end ms, title], ...] chapter markers, tags] of one file."""
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    duration = int((getattr(getattr(f, "info", None), "length", 0) or 0) * 1000) if f is not None else 0
    tags = dict(f.tags) if f is not None and f.tags else {}
    markers = _m4b_chapters(path) if path.suffix.lower() in MARKED else []
    return [duration, markers, {"title": _tag(tags, "album", "title"), "author": _tag(tags, "artist", "albumartist"),
                                "chapter": _tag(tags, "title")}]


def _m4b_chapters(path: Path) -> list[list]:
    """Chapter markers, via ffmpeg's metadata export (the image has no ffprobe)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                              "-f", "ffmetadata", "-"], capture_output=True, timeout=120).stdout.decode(errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    chapters = []
    for block in out.split("[CHAPTER]")[1:]:
        vals = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        try:
            num, den = (int(x) for x in vals.get("TIMEBASE", "1/1000").split("/"))
            start = int(vals["START"]) * 1000 * num // den
            end = int(vals["END"]) * 1000 * num // den
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        if end > start:
            chapters.append([start, end, vals.get("title", "").strip()])
    return chapters


class BookLibrary:
    """The books under one folder. Each file's length and chapters are cached
    (by path, size and mtime): a Zero takes a while to read a long m4b."""

    files = BOOK_FILES     # what counts as a book's audio

    def __init__(self, root: Path, cache_path: Path | None = None) -> None:
        self.root = Path(root)
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._books: dict[str, Book] = {}
        self._cache: dict = {}
        if cache_path is not None:
            try:
                data = json.loads(Path(cache_path).read_text())
                if data.get("version") == CACHE_VERSION:
                    self._cache = data.get("files", {})
            except (OSError, ValueError):
                pass

    def _file(self, p: Path, seen: set) -> list:
        st = p.stat()
        key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        seen.add(key)
        if key not in self._cache:
            self._cache[key] = _probe(p)
        return self._cache[key]

    def scan(self) -> list[Book]:
        books: dict[str, Book] = {}
        seen: set = set()
        if self.root.is_dir():
            for entry in self._book_roots():
                book = self._read(entry, seen)
                if book.chapters:
                    books[book.key] = book
        with self._lock:
            self._books = books
            self._cache = {k: v for k, v in self._cache.items() if k in seen}
        if self.cache_path is not None:
            try:
                write_atomic(Path(self.cache_path), json.dumps({"version": CACHE_VERSION, "files": self._cache}))
            except OSError:
                pass
        log.info("audiobooks: %d in %s", len(books), self.root)
        return list(books.values())

    def _book_roots(self) -> list[Path]:
        """Each book's folder or single file. A folder holding mp3s or m4as (and no
        other books' folders beside them) is one book, sub-folders and all; an
        .m4b, or an mp3 or m4a on its own, is a book by itself."""
        found = []

        def walk(d: Path, depth: int) -> None:
            kids = sorted((p for p in d.iterdir() if not p.name.startswith(".")), key=lambda p: _natural(p.name))
            files = [p for p in kids if p.is_file() and p.suffix.lower() in self.files]
            dirs = [p for p in kids if p.is_dir()]
            mp3s = [p for p in files if p.suffix.lower() != ".m4b"]      # chapter files: mp3s, m4as
            found.extend(p for p in files if p.suffix.lower() == ".m4b")
            if d != self.root and mp3s and (len(mp3s) > 1 or not dirs):
                found.append(d)                      # a folder of chapters (CD1/, CD2/... included)
                return
            found.extend(mp3s)                       # single-file mp3 books
            if depth < 4:
                for sub in dirs:
                    walk(sub, depth + 1)
        walk(self.root, 0)
        return found

    def _read(self, entry: Path, seen: set) -> Book:
        key = str(entry.relative_to(self.root))
        near = entry.parent.name if entry.parent != self.root else ""
        if entry.is_file():
            duration, markers, tags = self._file(entry, seen)
            chapters = [Chapter(entry, s, e, t or f"Chapter {i + 1}") for i, (s, e, t) in enumerate(markers)] \
                or [Chapter(entry, 0, duration, entry.stem)]
            title, author = tags["title"] or entry.stem, tags["author"] or near
        else:
            files = sorted((p for p in entry.rglob("*") if p.is_file() and p.suffix.lower() in self.files
                            and not any(part.startswith(".") for part in p.relative_to(entry).parts)),
                           key=lambda p: _natural(str(p.relative_to(entry))))
            chapters, first = [], None
            for p in files:
                duration, markers, tags = self._file(p, seen)
                first = first or tags
                if markers:
                    chapters += [Chapter(p, s, e, t or p.stem) for s, e, t in markers]
                elif duration:
                    chapters.append(Chapter(p, 0, duration, re.sub(r"^\d+\s*[-._]\s*", "", p.stem) or p.stem))
            title = (first or {}).get("title") or entry.name
            author = (first or {}).get("author") or near
        chapters = [c for c in chapters if c.length_ms > 0]
        offset = 0
        for c in chapters:
            c.offset_ms = offset
            offset += c.length_ms
        return Book(key, title, author, chapters)

    def get(self, key: str) -> Book | None:
        with self._lock:
            return self._books.get(key)

    def all(self) -> list[Book]:
        with self._lock:
            return list(self._books.values())


class KeptLibrary(BookLibrary):
    """The On demand things set to remember their place (`paths`, under the On
    demand folder): each is read as a book and played as one -- a file on its
    own, or a folder as one book, its files the chapters in order. Their keys
    start with KEPT, so they share the books' places file without clashing."""

    files = KEPT_FILES

    def __init__(self, root: Path, cache_path: Path | None = None, paths=()) -> None:
        super().__init__(root, cache_path)
        self.paths: list[str] = [p for p in paths if isinstance(p, str)]

    def _book_roots(self) -> list[Path]:
        return [self.root / p for p in self.paths if p and ".." not in p.split("/") and (self.root / p).exists()]

    def _read(self, entry: Path, seen: set) -> Book:
        book = super()._read(entry, seen)
        if entry.is_file():                      # one piece: its own title (a book's file is named for its album)
            tags = self._file(entry, seen)[2]
            book.title = tags.get("chapter") or entry.stem
            book.author = tags.get("author") or ""
        book.key = KEPT + book.key
        return book

    def key_for(self, path: str) -> str | None:
        """The kept thing a path under On demand is, or is in: its key, or None."""
        for p in sorted(self.paths, key=len, reverse=True):
            if path == p or path.startswith(p + "/"):
                return KEPT + p
        return None


class Positions:
    """Where each book was left: {key: {"pos_ms", "at"}} in a JSON file."""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._data: dict = {}
        if self.path is not None:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}

    def get(self, key: str) -> int:
        with self._lock:
            return int(self._data.get(key, {}).get("pos_ms", 0))

    def when(self, key: str) -> float:
        with self._lock:
            return float(self._data.get(key, {}).get("at", 0))

    def done(self, key: str) -> bool:
        with self._lock:
            return bool(self._data.get(key, {}).get("done"))

    def set(self, key: str, pos_ms: int, done: bool = False) -> None:
        with self._lock:
            self._data[key] = {"pos_ms": max(0, int(pos_ms)), "at": time.time(), **({"done": True} if done else {})}
            data = json.dumps(self._data)
        if self.path is not None:
            try:
                write_atomic(self.path, data)
            except OSError as e:
                log.warning("couldn't save the book's place: %s", e)
