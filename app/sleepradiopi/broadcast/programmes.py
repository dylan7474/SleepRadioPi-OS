"""Programmes: a running order of blocks the radio plays by itself.

A programme is a list of blocks, each holding some things -- albums, tracks,
folders, playlists, stations, audiobooks, podcast episodes, the show or an
artist list -- and a rule for how long it runs:

    "for"    a number of minutes
    "until"  a clock time
    "end"    until what's in it ends (the show has no end: an hour)
    "at"     starts at a clock time sharp, for a number of minutes -- the
             block before it plays on until then, and a block still playing
             is cut off (the news at 13:00)

and what happens after the last one ("then"): back to the show, fade out and
pause (like the sleep timer), or start again. A programme can also start by
itself at its start time on chosen days.

Each block plays through what the radio already does, so the DJ follows its
own setting: music (albums, tracks, playlists, folders) goes through the
show's queue, talked over as usual when the DJ is on; a station, a book, an
episode or a single On demand folder plays on its own with no DJ; the show or
an artist list is the show itself.

Kept in the config as "programmes". The Scheduler runs them: tick() once a
second (from its own thread on the radio; by hand in the tests).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta
from collections.abc import Callable

log = logging.getLogger(__name__)

MAX_PROGRAMMES = 30
MAX_BLOCKS = 40
MAX_ITEMS = 50
NAME_MAX = 40
RULES = ("for", "until", "end", "at")
THENS = ("show", "sleep", "repeat")
ITEM_KINDS = ("album", "track", "station", "playlist", "book", "podcast", "episode", "show", "list")
DEFAULT_MIN = 60
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _text(v, what, need=True, limit=200) -> str | None:
    if v is None and not need:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise ValueError(f"{what} must be some text")
    return " ".join(v.split())


def _hhmm(v, what) -> str:
    if not isinstance(v, str) or not _HHMM.match(v):
        raise ValueError(f"{what} must be a time like 13:00")
    return v


def _item(it) -> dict:
    """One thing in a block, as a reference the radio can find again."""
    if not isinstance(it, dict) or it.get("kind") not in ITEM_KINDS:
        raise ValueError(f"each thing in a block is one of: {', '.join(ITEM_KINDS)}")
    k = it["kind"]
    if k in ("album", "track"):
        root = it.get("root", "music")
        if root not in ("music", "ondemand"):
            raise ValueError("an album or track is in music or ondemand")
        key = "folder" if k == "album" else "path"
        path = it.get(key)
        if not isinstance(path, str) or len(path) > 1000 or ".." in path.split("/"):
            raise ValueError(f"a {k} needs its {key}")
        out = {"kind": k, "root": root, key: path.strip("/")}
        if k == "album" and it.get("deep") is True:
            out["deep"] = True
        return out
    if k == "station":
        return {"kind": k, "name": _text(it.get("name"), "a station's name"), "url": _text(it.get("url"), "a station's address", limit=2000)}
    if k == "playlist":
        return {"kind": k, "name": _text(it.get("name"), "a playlist's name")}
    if k == "book":
        return {"kind": k, "key": _text(it.get("key"), "a book", limit=1000)}
    if k == "podcast":
        return {"kind": k, "show": _text(it.get("show"), "a podcast")}
    if k == "episode":
        return {"kind": k, "show": _text(it.get("show"), "a podcast"), "guid": _text(it.get("guid"), "an episode", limit=2000)}
    if k == "list":
        return {"kind": k, "name": _text(it.get("name"), "an artist list's name")}
    return {"kind": "show", "artist": _text(it.get("artist"), "an artist", need=False)}


def _block(b) -> dict:
    if not isinstance(b, dict):
        raise ValueError("each block needs a name, things and a rule")
    rule = b.get("rule", "end")
    if rule not in RULES:
        raise ValueError(f"a block's rule is one of: {', '.join(RULES)}")
    items = b.get("items", [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(f"a block holds up to {MAX_ITEMS} things")
    out = {"name": _text(b.get("name") or "Untitled", "a block's name", limit=NAME_MAX),
           "items": [_item(i) for i in items], "rule": rule,
           "order": "shuffle" if b.get("order") == "shuffle" else "inorder"}
    if rule in ("for", "at"):
        m = b.get("min", DEFAULT_MIN)
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 24 * 60:
            raise ValueError("a block runs for 1 minute to 24 hours")
        out["min"] = m
    if rule == "at":
        out["at"] = _hhmm(b.get("at"), "a block's start time")
    if rule == "until":
        out["until"] = _hhmm(b.get("until"), "a block's end time")
    return out


def validate(programmes) -> list[dict]:
    """Check a list from the page or a settings file; returns it cleaned up.
    ValueError says what's wrong."""
    if not isinstance(programmes, list):
        raise ValueError("programmes must be a list")
    if len(programmes) > MAX_PROGRAMMES:
        raise ValueError(f"at most {MAX_PROGRAMMES} programmes")
    out, seen = [], set()
    for p in programmes:
        if not isinstance(p, dict):
            raise ValueError("each programme needs a name and blocks")
        name = _text(p.get("name"), "a programme's name", limit=NAME_MAX)
        if name.lower() in seen:
            raise ValueError(f"there are two programmes called {name}")
        seen.add(name.lower())
        blocks = p.get("blocks", [])
        if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
            raise ValueError(f"{name}: up to {MAX_BLOCKS} blocks")
        days = p.get("days", [])
        if not isinstance(days, list) or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
            raise ValueError(f"{name}: days are 0 (Monday) to 6 (Sunday)")
        then = p.get("then", "show")
        if then not in THENS:
            raise ValueError(f"{name}: then is one of {', '.join(THENS)}")
        out.append({"name": name, "start": _hhmm(p.get("start", "12:00"), f"{name}'s start time"),
                    "auto": p.get("auto") is True, "days": sorted(set(days)), "then": then,
                    "blocks": [_block(b) for b in blocks]})
    return out


def find(programmes: list[dict], name) -> dict | None:
    if not isinstance(name, str):
        return None
    key = " ".join(name.split()).lower()
    return next((p for p in programmes if p["name"].lower() == key), None)


def _next_clock(now: datetime, hhmm: str, not_before: datetime | None = None) -> datetime:
    """The next time the clock says hh:mm, from now (or from not_before)."""
    base = not_before or now
    h, m = map(int, hhmm.split(":"))
    t = base.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= base else t + timedelta(days=1)


class Scheduler:
    """Runs one programme at a time on the station. `wake` starts the speaker
    (a programme starting by itself plays even if the radio was paused);
    `sleep` fades it out and pauses it."""

    def __init__(self, station, now: Callable[[], datetime] = datetime.now,
                 wake: Callable[[], None] | None = None, sleep: Callable[[], None] | None = None) -> None:
        self.station = station
        self.now = now
        self.wake = wake
        self.sleep = sleep
        self.programmes: list[dict] = []
        self.run: dict | None = None      # the programme playing, and where it's got to
        self._lock = threading.RLock()
        self._auto_done: set[str] = set()   # "name@2026-09-30 12:00": started by itself already
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- the list ---------------------------------------------------------------------

    def set_programmes(self, entries) -> None:
        self.programmes = validate(entries)
        with self._lock:
            if self.run and not find(self.programmes, self.run["name"]):
                self.stop("it was deleted")

    def rename_refs(self, old_root: str, old: str, new_root: str, new: str) -> bool:
        """A file or folder was moved: blocks that pointed into it follow it."""
        changed = False
        for p in self.programmes:
            for b in p["blocks"]:
                for it in b["items"]:
                    key = "folder" if it["kind"] == "album" else "path" if it["kind"] == "track" else None
                    if key and it.get("root", "music") == old_root and (it[key] == old or it[key].startswith(old + "/")):
                        it["root"], it[key] = new_root, new + it[key][len(old):]
                        changed = True
        return changed

    # --- playing ----------------------------------------------------------------------

    def play(self, name: str) -> dict:
        p = find(self.programmes, name)
        if p is None:
            raise ValueError("there's no programme of that name")
        if not p["blocks"]:
            raise ValueError(f"{p['name']} is empty: put something in it first")
        with self._lock:
            self.run = {"name": p["name"], "prog": p, "i": -1, "ends": None, "expect": None, "started": self.now()}
            self._start_block(0)
        log.info("programme: %s", p["name"])
        return self.status()

    def stop(self, why: str = "stopped") -> bool:
        with self._lock:
            if self.run is None:
                return False
            log.info("programme %s: %s", self.run["name"], why)
            if why != "something else was chosen":
                self._drop_queued()
            self.run = None
            return True

    def status(self) -> dict | None:
        r = self.run
        if r is None:
            return None
        blocks = r["prog"]["blocks"]
        b = blocks[r["i"]] if 0 <= r["i"] < len(blocks) else None
        nxt = blocks[r["i"] + 1] if r["i"] + 1 < len(blocks) else None
        return {"name": r["name"], "index": r["i"], "of": len(blocks), "block": b["name"] if b else None,
                "started": r["started"].strftime("%H:%M"),
                "block_started": r["block_started"].strftime("%H:%M") if r.get("block_started") else None,
                "until": r["ends"].strftime("%H:%M") if r["ends"] else None,
                "waiting": r.get("waiting", False),
                "next": (nxt["name"] + (f" at {nxt['at']}" if nxt["rule"] == "at" else "")) if nxt else None}

    # --- the clock --------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="programmes", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("programme tick failed")

    def tick(self) -> None:
        now = self.now()
        with self._lock:
            self._auto_start(now)
            r = self.run
            if r is None:
                return
            if self._taken_over():
                self.stop("something else was chosen")
                return
            blocks = r["prog"]["blocks"]
            nxt = blocks[r["i"] + 1] if r["i"] + 1 < len(blocks) else None
            # an "at" block cuts in at its time, whatever's playing
            if nxt is not None and nxt["rule"] == "at" and r["at_next"] is not None and now >= r["at_next"]:
                self._start_block(r["i"] + 1)
                return
            done = (r["ends"] is not None and now >= r["ends"]) or (r["ends"] is None and self._content_over())
            if r.get("waiting"):
                return                        # filling in with the show until the "at" block's time
            if not done:
                return
            if nxt is None:
                self._finish()
            elif nxt["rule"] == "at" and r["at_next"] is not None and now < r["at_next"]:
                r["waiting"] = True           # too early for it: the show until then
                self._drop_queued()
                r["expect"] = None
                self.station.tune(None)
                log.info("programme %s: the show until %s", r["name"], nxt["at"])
            else:
                self._start_block(r["i"] + 1)

    def _auto_start(self, now: datetime) -> None:
        for p in self.programmes:
            if not p["auto"] or (p["days"] and now.weekday() not in p["days"]):
                continue
            h, m = map(int, p["start"].split(":"))
            if (now.hour, now.minute) != (h, m):
                continue
            key = f"{p['name']}@{now:%Y-%m-%d %H:%M}"
            if key in self._auto_done or not p["blocks"]:
                continue
            self._auto_done.add(key)
            log.info("programme %s starts by itself", p["name"])
            self.play(p["name"])
            if self.wake:
                self.wake()

    # --- blocks -----------------------------------------------------------------------

    def _drop_queued(self) -> None:
        """A music block's time is up: the rest of its songs don't linger in the show."""
        exp = self.run.get("expect") if self.run else None
        if exp and exp[0] == "run":
            pl = self.station.playlist_status()
            if pl is not None and pl["name"] == exp[1]:
                self.station.stop_playlist()

    def _start_block(self, i: int) -> None:
        r = self.run
        self._drop_queued()
        prog, now = r["prog"], self.now()
        b = prog["blocks"][i]
        r["i"], r["waiting"], r["block_started"] = i, False, now
        if b["rule"] in ("for", "at"):
            r["ends"] = now + timedelta(minutes=b["min"])
        elif b["rule"] == "until":
            r["ends"] = _next_clock(now, b["until"])
        else:
            r["ends"] = None if self._has_end(b) else now + timedelta(minutes=DEFAULT_MIN)
        nxt = prog["blocks"][i + 1] if i + 1 < len(prog["blocks"]) else None
        r["at_next"] = None
        if nxt and nxt["rule"] == "at":
            at = _next_clock(now, nxt["at"])
            # its time has gone today (played late): it just follows on, like any block
            r["at_next"] = at if at - now <= timedelta(hours=12) else None
        try:
            r["expect"] = self._play_block(b)
        except (ValueError, LookupError) as e:          # e.g. a station gone: skip it
            log.warning("programme %s: block %s can't play (%s): skipped", r["name"], b["name"], e)
            r["expect"], r["ends"] = None, now
        log.info("programme %s: block %d/%d %s until %s", r["name"], i + 1, len(prog["blocks"]), b["name"],
                 r["ends"].strftime("%H:%M") if r["ends"] else "it ends")

    @staticmethod
    def _has_end(b: dict) -> bool:
        return any(it["kind"] not in ("show", "list", "station") for it in b["items"])

    def _play_block(self, b: dict):
        """Start a block on the station; returns what to watch for its end (and
        for something else being chosen): ("source", dict) or ("run", name) or None."""
        st, items = self.station, b["items"]
        if not items:
            st.tune(None)
            return None
        first = lambda k: next((it for it in items if it["kind"] == k), None)
        if (s := first("station")):
            st.tune({"kind": "radio", "name": s["name"], "url": s["url"]})
            return ("source", st.source)
        if (bk := first("book")):
            st.play_book(bk["key"])
            return ("source", st.source)
        if (ep := first("episode")):
            st.play_episode(ep["show"], guid=ep["guid"])
            return ("source", st.source)
        if (pc := first("podcast")):
            st.play_episode(pc["show"])
            return ("source", st.source)
        music = [it for it in items if it["kind"] in ("album", "track", "playlist")]
        if music:
            if len(music) == 1 and music[0]["kind"] == "album" and music[0]["root"] == "ondemand" and b["order"] == "inorder":
                a = music[0]                  # On demand on its own: straight through, no DJ
                st.play_album(root="ondemand", folder=a["folder"], deep=a.get("deep", False))
                return ("source", st.source)
            tracks = self._tracks(music)
            if not tracks:
                raise LookupError("nothing in it is on the radio")
            if b["order"] == "shuffle":
                random.shuffle(tracks)
            st.play_tracks_as(b["name"], tracks)
            return ("run", b["name"])
        show = first("list") or first("show")
        if show["kind"] == "list":
            st.set_profile(show["name"])
        else:
            st.set_artist(show.get("artist"))
        if st.source is not None:
            st.tune(None)
        return ("show", None)

    def _tracks(self, music: list[dict]) -> list:
        st, out = self.station, []
        for it in music:
            if it["kind"] == "track":
                t = st._by_ref(it["root"], it["path"])
                out += [t] if t is not None else []
            elif it["kind"] == "album":
                a = st._album_by_folder(it["folder"], it["root"], it.get("deep", False))
                out += list(a["tracks"]) if a else []
            else:
                from . import playlists as pl
                p = pl.find(st.playlists, it["name"])
                out += [t for root, path in (p["tracks"] if p else []) if (t := st._by_ref(root, path)) is not None]
        return out

    def _content_over(self) -> bool:
        exp = self.run["expect"]
        if exp is None:
            return True
        kind, what = exp
        if kind == "source":
            return self.station.source is None
        if kind == "run":
            return self.station.playlist_status() is None
        return False

    def _taken_over(self) -> bool:
        """Something other than the programme was chosen (a station, an album,
        another playlist...): the programme gives way."""
        exp, st = self.run["expect"], self.station
        if self.run.get("waiting"):           # the show, filling in: anything chosen now wins
            return st.source is not None
        if exp is None:
            return False
        kind, what = exp
        if kind == "source":
            return st.source is not None and st.source is not what
        if kind == "run":
            pl = st.playlist_status()
            return st.source is not None or (pl is not None and pl["name"] != what)
        return st.source is not None

    def _finish(self) -> None:
        r = self.run
        then = r["prog"]["then"]
        log.info("programme %s: over (%s)", r["name"], then)
        if then == "repeat":
            self._start_block(0)
            return
        self._drop_queued()
        self.run = None
        if self.station.source is not None:
            self.station.tune(None)
        if then == "sleep" and self.sleep:
            self.sleep()
