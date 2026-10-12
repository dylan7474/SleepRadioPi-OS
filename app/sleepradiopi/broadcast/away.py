"""Messages from away: the DJ's messages kept in a private file somewhere else, which the radio looks at now and then.

Messages (messages.py) are written on the radio's own page, and a radio in
a care home can't be reached from home: its Wi-Fi lets nothing in. So the
radio does the asking. Its owner keeps the messages in a document of their
own -- a Google Doc, shared so that "anyone with the link" can view it, is
the easy one: it's edited on a phone, and its link never changes however
often it's edited -- and gives the radio the link (the page's Messages
window). The radio fetches it every EVERY_MIN and reads those messages
with its own, as if they'd been typed in.

The link is the secret: anyone who has it can read the messages, nobody can
write them without the document's owner's say. Nothing but messages comes
from it -- words for the DJ and when to say them, checked like any others
(messages.validate()); no setting of the radio's can be changed from away.

The document is plain words. Each paragraph (a blank line between) is a
message, said like the radio's own: every 15 minutes in turn, 8 till 9,
the day and date first. A paragraph can begin with notes in square brackets
to say otherwise:

    You're at Willow Court, and you're safe here.

    [at 10:00 16:00] [until 2026-10-18] Dylan is coming to see you on Sunday.

    [every 60] [from 09:00 to 18:00] [no date] Lunch is at half past twelve.

    [days sat sun] [theme Carisbrooke] There's a film in the lounge tonight.

    # a line beginning with # is a note to yourself

[at ...] times of day; [every N] minutes (15, 20, 30, 60, 120); [from HH:MM
to HH:MM]; [until YYYY-MM-DD] its last day; [days mon tue ...]; [date
MM-DD] that day each year; [no date] not the day and date first; [theme
NAME] only on that theme's station (none: on whichever is playing); [off]
kept but not said. A note that isn't understood is said to the page (and
the message is left out, rather than said at the wrong times). A file that
begins with { or [ is taken as JSON instead: the "list" of messages.py.

What was last fetched is kept (keep_in()), so a restart, or a week with no
internet, says the same messages; "until" is what stops a stale "coming on
Sunday". An empty document means no messages from away; a document that
can't be fetched, or isn't messages at all, changes nothing.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from sleepradiopi.broadcast import messages
from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

EVERY = (15, 30, 60, 180)            # minutes between looks
EVERY_MIN = 30
SIZE_MOST = 64 * 1024                # a document bigger than this isn't messages
URL_MOST = 500
RETRY_S = 300.0                      # after a look that failed, not again for this long
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"url": "", "every_min": EVERY_MIN}


def validate(cfg) -> dict:
    """The setting ("messages_away"): {"url", "every_min"}. ValueError says what's wrong."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("messages_away must be an object")
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    url = out["url"]
    if not isinstance(url, str) or len(url) > URL_MOST:
        raise ValueError("the link is an address, like https://docs.google.com/document/d/...")
    url = url.strip()
    if url and not re.match(r"https?://[^\s/]+\.[^\s/]+(/\S*)?$", url):
        raise ValueError("the link is an address, like https://docs.google.com/document/d/...")
    if out["every_min"] not in EVERY or isinstance(out["every_min"], bool):
        raise ValueError(f"every_min is one of {', '.join(map(str, EVERY))}")
    return {"url": url, "every_min": out["every_min"]}


def direct(url: str) -> str:
    """The address the words themselves come from, for the links people copy: a Google Doc's "share" link -> its
    plain-text export; a file on Google Drive -> its download; Dropbox -> the file, not its page; a GitHub gist's
    page -> its raw text. Anything else is fetched as it is."""
    m = re.match(r"https://docs\.google\.com/document/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
    m = re.match(r"https://drive\.google\.com/file/d/([\w-]+)", url) or re.match(r"https://drive\.google\.com/open\?id=([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if re.match(r"https://(www\.)?dropbox\.com/", url):
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    m = re.match(r"https://gist\.github\.com/([\w-]+)/([0-9a-f]+)/?$", url)
    if m:
        return f"https://gist.githubusercontent.com/{m.group(1)}/{m.group(2)}/raw"
    return url


_NOTE = re.compile(r"\s*\[([^\]\n]*)\]")
_TIME = r"\d{1,2}[:.]\d{2}"


def _note(entry: dict, note: str) -> None:
    """One [note] of a paragraph, put into the message. ValueError if it isn't understood."""
    words = note.replace(",", " ").split()
    if not words:
        raise ValueError("an empty [ ]")
    key, rest = words[0].lower(), words[1:]
    clock = lambda t: t.replace(".", ":")
    if key == "at" and rest and all(re.fullmatch(_TIME, t) for t in rest):
        entry["times"] = entry.get("times", []) + [clock(t) for t in rest]
    elif key == "every" and len(rest) in (1, 2) and rest[0].isdigit():
        entry["every"] = int(rest[0])
    elif key == "from" and len(rest) == 3 and rest[1].lower() == "to" and re.fullmatch(_TIME, rest[0]) and re.fullmatch(_TIME, rest[2]):
        entry["from"], entry["to"] = clock(rest[0]), clock(rest[2])
    elif key == "until" and len(rest) == 1:
        entry["until"] = rest[0]
    elif key == "days" and rest and all(d.lower()[:3] in DAY_NAMES for d in rest):
        entry["days"] = [DAY_NAMES.index(d.lower()[:3]) for d in rest]
    elif key == "date" and len(rest) == 1:
        entry["date"] = rest[0]
    elif key == "no" and [r.lower() for r in rest] == ["date"]:
        entry["date_first"] = False
    elif key in ("theme", "station") and rest:
        entry["station"] = " ".join(rest)
    elif key == "off" and not rest:
        entry["off"] = True
    else:
        raise ValueError(f"[{note}] isn't a note I know")


def parse(text: str) -> tuple[list[dict], list[str]]:
    """A document's words -> (the messages, as messages.validate() leaves them but with no station unless one is
    named; what was wrong with any paragraph left out). ValueError if it isn't a document of messages at all."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text or re.search(r"<\s*(html|!doctype|head|body)\b", text[:2000], re.I):
        raise ValueError("the link gives a web page, not the document's words: is it shared so that anyone with the link can view it?")
    if text.lstrip()[:1] in ("{", "["):              # JSON, perhaps (a paragraph's [note] begins the same way)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, (dict, list)):
            items = data.get("list") if isinstance(data, dict) else data
            return [_tidy(m) for m in messages.validate({"list": items})["list"]], []
    out, wrong = [], []
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip() and not ln.strip().startswith("#")]
        body = " ".join(lines)
        if not body:
            continue
        entry: dict = {}
        try:
            while (m := _NOTE.match(body)):
                _note(entry, m.group(1))
                body = body[m.end():]
            entry["text"] = body.strip()
            if not entry["text"]:
                raise ValueError("notes with no message after them")
            out.append(_tidy(messages.validate({"list": [entry]})["list"][0], named="station" in entry))
        except ValueError as e:
            wrong.append(f"{str(e)} ({' '.join(lines)[:50]}…)" if len(" ".join(lines)) > 50 else f"{e} ({' '.join(lines)})")
        if len(out) > messages.MAX_MESSAGES:
            raise ValueError(f"up to {messages.MAX_MESSAGES} messages")
    return out, wrong


def _tidy(m: dict, named: bool | None = None) -> dict:
    """A validated message as one from away: marked, and with a station only if the document named one."""
    m = {**m, "away": True}
    if named is False:
        m.pop("station", None)
    return m


def _fetch(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(SIZE_MOST + 1)
    if len(data) > SIZE_MOST:
        raise ValueError("the document is too big to be messages")
    return data.decode("utf-8", "replace")


class Away:
    """The messages from away: fetched now and then (in a thread of its own) and kept. on_change(list) is told
    whenever they change."""

    def __init__(self, cfg: dict | None = None, fetch: Callable[[str], str] = _fetch, clock: Callable[[], float] = time.time,
                 on_change: Callable[[list], None] = lambda items: None) -> None:
        try:
            self.cfg = validate(cfg)
        except ValueError as e:
            log.warning("messages from away ignored: %s", e)
            self.cfg = dict(DEFAULTS)
        self._fetch, self._clock, self.on_change = fetch, clock, on_change
        self._lock = threading.Lock()
        self._file: Path | None = None
        self.items: list[dict] = []
        self._state: dict = {}               # {"t": when last fetched, "tried": when last tried, "error"?, "wrong": [...]}
        self._busy = False
        self._stop = threading.Event()

    # --- kept -------------------------------------------------------------------------------

    def keep_in(self, file: Path | None) -> None:
        """Where what's fetched is kept, and what's there read back (if it's from the same link)."""
        self._file = file
        if file is None:
            return
        try:
            kept = json.loads(file.read_text())
            if kept.get("url") != self.cfg["url"] or not self.cfg["url"]:
                return
            items = [_tidy(m, named="station" in m) for m in messages.validate({"list": kept["list"]})["list"]]
            for m, raw in zip(items, kept["list"]):
                if "station" not in raw:
                    m.pop("station", None)
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("messages from away: what was kept in %s can't be read (%s)", file, e)
            return
        with self._lock:
            self.items = items
            self._state = {"t": float(kept.get("t") or 0) or None, "wrong": [str(w) for w in kept.get("wrong", [])][:10]}
        self.on_change(items)

    def _save(self) -> None:
        if self._file is None:
            return
        with self._lock:
            text = json.dumps({"url": self.cfg["url"], "t": self._state.get("t"), "wrong": self._state.get("wrong", []), "list": self.items})
        try:
            write_atomic(self._file, text)
        except OSError as e:
            log.warning("messages from away weren't kept (%s)", e)

    # --- the link ---------------------------------------------------------------------------

    def set(self, cfg: dict) -> None:
        """A new link (or none), or how often. ValueError if wrong. A changed link forgets the old one's messages."""
        cfg = validate(cfg)
        with self._lock:
            changed = cfg["url"] != self.cfg["url"]
            self.cfg = cfg
            if changed:
                self.items, self._state = [], {}
        if changed:
            self._save()
            self.on_change([])

    def status(self) -> dict:
        with self._lock:
            return {**self.cfg, "count": len(self.items), "list": [dict(m) for m in self.items], "busy": self._busy,
                    **{k: v for k, v in self._state.items() if v not in (None, [])}}

    def look(self) -> dict:
        """Fetch the document now and take its messages. -> status(). Errors are kept in it ("error"), not raised."""
        with self._lock:
            url = self.cfg["url"]
            if not url or self._busy:
                return self._status_unlocked()
            self._busy = True
        now = self._clock()
        try:
            items, wrong = parse(self._fetch(direct(url)))
            with self._lock:
                same = items == self.items
                if url == self.cfg["url"]:            # (the link wasn't changed meanwhile)
                    self.items = items
                    self._state = {"t": now, "tried": now, "wrong": wrong[:10]}
            self._save()
            if not same:
                log.info("messages from away: %d (%d paragraphs left out)", len(items), len(wrong))
                self.on_change(items)
        except (OSError, ValueError, urllib.error.URLError) as e:
            why = str(e) if isinstance(e, ValueError) else "the document couldn't be fetched"
            if isinstance(e, urllib.error.HTTPError):
                why = ("the link isn't shared: set it so that anyone with the link can view it" if e.code in (401, 403)
                       else "there's no document at that link" if e.code in (404, 410) else why)
            log.info("messages from away: %s (%s)", why, e)
            with self._lock:
                self._state = {**self._state, "tried": now, "error": why}
        finally:
            with self._lock:
                self._busy = False
        return self.status()

    def _status_unlocked(self) -> dict:
        return {**self.cfg, "count": len(self.items), "list": [dict(m) for m in self.items], "busy": self._busy,
                **{k: v for k, v in self._state.items() if v not in (None, [])}}

    def due(self) -> bool:
        """Is it time to look again?"""
        with self._lock:
            if not self.cfg["url"] or self._busy:
                return False
            tried, got = self._state.get("tried") or 0, self._state.get("t") or 0
            now = self._clock()
            if self._state.get("error"):              # (a look that failed: again soon, not after the whole wait)
                return now - tried >= RETRY_S
            return now - max(got, tried) >= self.cfg["every_min"] * 60

    def start(self) -> None:
        threading.Thread(target=self.run, name="messages-away", daemon=True).start()

    def run(self, wait: Callable[[float], bool] | None = None) -> None:
        """(a thread) Look whenever it's due."""
        wait = wait or self._stop.wait
        while not wait(20.0):
            try:
                if self.due():
                    self.look()
            except Exception:
                log.exception("messages from away")


remote = Away()                      # the radio's own; main.py gives it the setting, where to keep things, and the station
