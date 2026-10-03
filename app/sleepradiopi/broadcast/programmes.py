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

Beside the running order, switches: the noise, the DJ or the night buttons on for a stretch of
the programme ("switches": [{"what": "noise", "from": 0, "min": 60}], minutes
from its start). On at the start of the stretch and off at its end, whatever
they were before -- apart from the blocks, so they overlap them freely. A
stretch that has started runs to its end even if the programme gives way; one
not yet started is dropped; Stop ends them all at once.

and what happens after the last one ("then"): back to the show, fade out and
pause (like the sleep timer), or start again. A programme can also start by
itself at its start time on chosen days.

Each block plays through what the radio already does, so the DJ follows its
own setting: music (albums, tracks, playlists, folders) goes through the
show's queue, talked over as usual when the DJ is on; a station, a book, an
episode or a single On demand folder plays on its own with no DJ; the show or
an artist list is the show itself.

Programme mode (quiet = True, the "programme_mode" setting): the radio is
silent unless a programme is on -- an alarm clock. Gaps are silent, and when a
programme ends (unless it chains on or repeats) the radio pauses.

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
THENS = ("show", "stop", "sleep", "repeat", "keep", "chain")
GAPS = ("show", "silence")
ITEM_KINDS = ("album", "track", "station", "playlist", "book", "podcast", "episode", "show", "list",
              "message", "jingle", "action")
INSTANT = ("message", "jingle", "action")     # happen at once: said, played or done, then on
ACTIONS = ("time", "news", "sleep", "pips", "address",   # (what a button can do, but on or off, not a switch:
           "noise_on", "noise_off", "dj_on", "dj_off")     #  at a set time, a switch could go either way)
SWITCHES = ("noise", "dj", "night")          # on for a stretch, then off (not in the running order);
                                             # night: the buttons' night set for the stretch, then the day set
MAX_SWITCHES = 10
EVERY = (15, 30, 60)                         # repeating within the day, from start to until
MOMENT_LEAD = timedelta(seconds=60)          # a moment with the time gets ready this early (the voice is slow)
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
    if k == "message":
        return {"kind": k, "text": _text(it.get("text"), "a message", limit=1000)}
    if k == "jingle":
        path = it.get("path")
        if not isinstance(path, str) or not path or len(path) > 1000 or ".." in path.split("/"):
            raise ValueError("a jingle needs its file")
        return {"kind": k, "path": path.strip("/")}
    if k == "action":
        if it.get("action") not in ACTIONS:
            raise ValueError(f"an action is one of: {', '.join(ACTIONS)}")
        return {"kind": k, "action": it["action"]}
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


def _switch(w) -> dict:
    if not isinstance(w, dict) or w.get("what") not in SWITCHES:
        raise ValueError(f"a switch is one of: {', '.join(SWITCHES)}")
    f, m = w.get("from", 0), w.get("min", DEFAULT_MIN)
    for v, lo, what in ((f, 0, "starts 0 minutes to 24 hours in"), (m, 1, "lasts 1 minute to 24 hours")):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= 24 * 60:
            raise ValueError(f"a switch {what}")
    return {"what": w["what"], "from": f, "min": m}


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
        gap = p.get("gap", "show")
        if isinstance(gap, dict):
            gap = _item(gap)
            if gap["kind"] in INSTANT:
                raise ValueError(f"{name}: the gaps need something that plays")
        elif gap not in GAPS:
            raise ValueError(f"{name}: in the gaps, the show, silence or something to play")
        entry = {"name": name, "start": _hhmm(p.get("start", "12:00"), f"{name}'s start time"),
                 "auto": p.get("auto") is True, "days": sorted(set(days)), "then": then, "gap": gap,
                 "blocks": [_block(b) for b in blocks]}
        switches = p.get("switches") or []
        if not isinstance(switches, list) or len(switches) > MAX_SWITCHES:
            raise ValueError(f"{name}: up to {MAX_SWITCHES} switches")
        if switches:
            entry["switches"] = [_switch(w) for w in switches]
            first = min(w["from"] for w in entry["switches"])
            if not entry["blocks"] and first:   # only switches: it starts when the first one does
                h, m = map(int, entry["start"].split(":"))
                t = (h * 60 + m + first) % 1440
                entry["start"] = f"{t // 60:02d}:{t % 60:02d}"
                for w in entry["switches"]:
                    w["from"] -= first
        if p.get("wake") is False:
            entry["wake"] = False
        every = p.get("every")
        if every not in (None, 0):
            if every not in EVERY or isinstance(every, bool):
                raise ValueError(f"{name}: repeats every {', '.join(map(str, EVERY))} minutes")
            entry["every"] = every
            entry["until"] = _hhmm(p.get("until") or "23:59", f"{name}'s last time")             # starts by itself only if the radio's playing (a pause wins)
        if entry["auto"] and p.get("once") is True:
            entry["once"] = True              # armed for its next start only, then off
        if then == "chain":
            entry["chain"] = _text(p.get("chain"), f"{name}: the programme to play next", limit=NAME_MAX)
        out.append(entry)
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
                 wake: Callable[[], None] | None = None, sleep: Callable[[], None] | None = None,
                 pause: Callable[[], None] | None = None, say: Callable[[str], None] | None = None,
                 jingle: Callable[[str], None] | None = None, action: Callable[[str], None] | None = None) -> None:
        self.station = station
        self.now = now
        self.wake = wake              # the speaker on
        self.sleep = sleep            # fade out and pause
        self.pause = pause            # pause at once
        self.say = say                # the DJ says a message
        self.jingle = jingle          # a jingle's file (in the jingles folder) played
        self.action = action          # "time", "news", "sleep": as the radio's buttons do them
        self.on_change: Callable[[list], None] | None = None   # the list changed by itself (a one-off start): save it
        self.quiet = False            # programme mode: silent unless a programme is on
        self.paused: Callable[[], bool] = lambda: False   # is the radio paused (by a person)?
        # the time signal: (when, pips, speak) -- the pips' long one on the minute, then the time said
        self.time_signal: Callable[[datetime, bool, bool], None] | None = None
        self.programmes: list[dict] = []
        self.run: dict | None = None      # the programme playing, and where it's got to
        self.spans: list[dict] = []       # switches due or on: {"prog", "what", "on", "off", "is_on"}
        self._lock = threading.RLock()
        self._auto_done: set[str] = set()   # "name@2026-09-30 12:00": started by itself already
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- the list ---------------------------------------------------------------------

    def set_quiet(self, on: bool) -> None:
        """Programme mode on or off. On, with no programme playing: quiet at once."""
        self.quiet = bool(on)
        log.info("programme mode %s", "on" if self.quiet else "off")
        if self.quiet and self.run is None and self.pause:
            self.pause()

    def set_programmes(self, entries) -> None:
        self.programmes = validate(entries)
        with self._lock:
            if self.run and not find(self.programmes, self.run["name"]):
                self.stop("it was deleted")

    def rename_theme(self, old: str, new: str) -> bool:
        """A theme was renamed: blocks that play it follow it."""
        changed = False
        for p in self.programmes:
            items = [it for b in p["blocks"] for it in b["items"]] + ([p["gap"]] if isinstance(p.get("gap"), dict) else [])
            for it in items:
                if it.get("kind") == "list" and (it.get("name") or "").lower() == old.lower():
                    it["name"] = new
                    changed = True
        return changed

    def artists_to_themes(self, theme_for) -> bool:
        """Artist radio is retired: a block (or gap) that played one artist plays
        that artist's one-artist theme."""
        changed = False
        for p in self.programmes:
            items = [it for b in p["blocks"] for it in b["items"]] + ([p["gap"]] if isinstance(p.get("gap"), dict) else [])
            for it in items:
                if it.get("kind") == "show" and it.get("artist"):
                    name = theme_for(it["artist"])
                    if name:
                        it.clear()
                        it.update({"kind": "list", "name": name})
                    else:
                        it.pop("artist")
                    changed = True
        return changed

    def rename_playlist(self, old: str, new: str) -> bool:
        """A playlist was renamed: blocks that play it follow it."""
        changed = False
        for p in self.programmes:
            items = [it for b in p["blocks"] for it in b["items"]] + ([p["gap"]] if isinstance(p.get("gap"), dict) else [])
            for it in items:
                if it.get("kind") == "playlist" and (it.get("name") or "").lower() == old.lower():
                    it["name"] = new
                    changed = True
        if self.run is not None and self.run.get("expect") == ("run", old):
            self.run["expect"] = ("run", new)     # (a block playing it: still its own)
        return changed

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
        if not p["blocks"] and not p.get("switches"):
            raise ValueError(f"{p['name']} is empty: put something in it first")
        with self._lock:
            now = self.now()
            self._arm_switches(p, now)
            if self._overlay(p):              # only moments and switches: over whatever's on
                for b in p["blocks"]:
                    self._instants(b, now)
                self._switch_now(now)
                log.info("programme %s: over what's on", p["name"])
                return self.status()
            self.run = {"name": p["name"], "prog": p, "i": -1, "ends": None, "expect": None, "started": now}
            first = p["blocks"][0]
            at = _next_clock(now, first["at"]) if first["rule"] == "at" else None
            if at is not None and timedelta(minutes=1) <= at - now <= timedelta(hours=12):
                # its first block is at a later time (e.g. chained on from another programme):
                # the gaps' choice (silence, the show, something) until then
                self.run.update(at_next=at, waiting=True)
                self._drop_queued()
                self._fill_gap()
                log.info("programme %s: waiting for %s", p["name"], first["at"])
            else:
                self._start_block(0)
            self._switch_now(now)
        log.info("programme: %s", p["name"])
        return self.status()

    def stop(self, why: str = "stopped", name: str | None = None) -> bool:
        """Stop the programme playing (Stop: its switches go off too). With a name:
        that programme -- its switches, even when only they are on."""
        with self._lock:
            if name is not None and not (self.run and self.run["name"].lower() == name.lower()):
                p = find(self.programmes, name)
                self._end_switches(p["name"] if p else name, True)
                return False
            if why != "something else was chosen":
                self._end_switches(self.run["name"] if self.run else None, why == "stopped")
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
        b = blocks[r["i"]] if 0 <= r["i"] < len(blocks) else None     # (-1: waiting for the first block)
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
            self._switch_now(now)
            r = self.run
            if r is None:
                return
            if self._taken_over():
                self._end_switches(r["name"], False)   # (started ones run on to their end)
                self.stop("something else was chosen")
                return
            blocks = r["prog"]["blocks"]
            nxt = blocks[r["i"] + 1] if r["i"] + 1 < len(blocks) else None
            # an "at" block cuts in at its time, whatever's playing
            lead = MOMENT_LEAD if nxt is not None and self._is_moment(nxt) else timedelta(0)
            if nxt is not None and nxt["rule"] == "at" and r["at_next"] is not None and now >= r["at_next"] - lead:
                self._start_block(r["i"] + 1, due=r["at_next"])
                return
            done = (r["ends"] is not None and now >= r["ends"]) or (r["ends"] is None and self._content_over())
            if r.get("waiting"):
                return                        # filling the gap until the "at" block's time
            if not done:
                return
            if nxt is None:
                self._finish()
            elif nxt["rule"] == "at" and r["at_next"] is not None and now < r["at_next"]:
                r["waiting"] = True           # too early for it: the gap until then
                self._drop_queued()
                self._fill_gap()
                log.info("programme %s: the gap until %s", r["name"], nxt["at"])
            else:
                self._start_block(r["i"] + 1)

    @staticmethod
    def _is_moment(b: dict) -> bool:
        return bool(b["items"]) and all(it["kind"] in INSTANT for it in b["items"])

    def _moments_only(self, p: dict) -> bool:
        return bool(p["blocks"]) and all(self._is_moment(b) for b in p["blocks"])

    def _overlay(self, p: dict) -> bool:
        """Only moments and switches (or only switches): it plays over what's on."""
        return all(self._is_moment(b) for b in p["blocks"]) and bool(p["blocks"] or p.get("switches"))

    # --- switches: the noise or the DJ on for a stretch -------------------------------

    def _arm_switches(self, p: dict, t0: datetime) -> None:
        """The programme starts at t0: its switches' stretches, from then. Started
        again, its stretches not yet begun are replaced (ones already on run on)."""
        self.spans = [s for s in self.spans if s["prog"] != p["name"] or s["is_on"]]
        for w in p.get("switches", []):
            on = t0 + timedelta(minutes=w["from"])
            self.spans.append({"prog": p["name"], "what": w["what"], "on": on,
                               "off": on + timedelta(minutes=w["min"]), "is_on": False})

    def _switch(self, what: str, on: bool) -> None:
        log.info("programme: %s %s", what, "on" if on else "off")
        if self.action:
            try:
                self.action(f"{what}_{'on' if on else 'off'}")
            except Exception:
                log.exception("programme: %s didn't switch", what)

    def _switch_now(self, now: datetime) -> None:
        """On at a stretch's start, off at its end -- whatever it was before."""
        for s in list(self.spans):
            if not s["is_on"] and now >= s["on"]:
                if now >= s["off"]:              # (missed it altogether: nothing)
                    self.spans.remove(s)
                    continue
                s["is_on"] = True
                self._switch(s["what"], True)
            if s["is_on"] and now >= s["off"]:
                self.spans.remove(s)
                if not any(o["is_on"] and o["what"] == s["what"] for o in self.spans):
                    self._switch(s["what"], False)   # (unless another stretch keeps it on)

    def _end_switches(self, name: str | None, now_too: bool) -> None:
        """A programme's stretches not yet begun are dropped; now_too (Stop): the
        ones on end at once, switched off."""
        for s in list(self.spans):
            if name is not None and s["prog"] != name:
                continue
            if s["is_on"] and not now_too:
                continue
            self.spans.remove(s)
            if s["is_on"] and not any(o["is_on"] and o["what"] == s["what"] for o in self.spans):
                self._switch(s["what"], False)

    def switches_status(self) -> list[dict]:
        return [{"programme": s["prog"], "what": s["what"], "on": s["is_on"],
                 "from": s["on"].strftime("%H:%M"), "until": s["off"].strftime("%H:%M")} for s in self.spans]

    def _times_today(self, p: dict, day: datetime) -> list[datetime]:
        """When an armed programme starts on this day (several, repeating every N minutes)."""
        h, m = map(int, p["start"].split(":"))
        first = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if not p.get("every"):
            return [first]
        uh, um = map(int, p["until"].split(":"))
        last = day.replace(hour=uh, minute=um, second=0, microsecond=0)
        out, t = [], first
        while t <= last:
            out.append(t)
            t += timedelta(minutes=p["every"])
        return out

    def _auto_start(self, now: datetime) -> None:
        for p in self.programmes:
            if not p["auto"] or not (p["blocks"] or p.get("switches")):
                continue
            moments = self._overlay(p)
            lead = MOMENT_LEAD if moments and p["blocks"] else timedelta(0)   # (the voice is slow; a switch isn't)
            for due in self._times_today(p, now) + self._times_today(p, now + timedelta(days=1)):
                if p["days"] and due.weekday() not in p["days"]:
                    continue
                if not (due - lead <= now < due - lead + timedelta(seconds=50)):
                    continue
                key = f"{p['name']}@{due:%Y-%m-%d %H:%M}"
                if key in self._auto_done:
                    continue
                self._auto_done.add(key)
                skip = p.get("wake") is False and self.paused()
                log.info("programme %s %s at %s%s", p["name"], "skipped: the radio's paused" if skip else "starts by itself",
                         f"{due:%H:%M}", " (once)" if p.get("once") else "")
                if p.get("once"):             # a one-off: not again (it had its turn)
                    p["auto"] = False
                    p.pop("once", None)
                    if self.on_change:
                        try:
                            self.on_change(self.programmes)
                        except Exception:
                            log.exception("couldn't save the programmes")
                if skip:
                    continue
                if moments:                   # a talking clock and the like: over whatever's on
                    for b in p["blocks"]:
                        self._instants(b, due)
                    self._arm_switches(p, due)
                    continue
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

    def _fill_gap(self) -> None:
        """Nothing in the programme for now: the show, silence, or its gap filler."""
        r, gap = self.run, self.run["prog"].get("gap", "show")
        if self.quiet and gap == "show":
            gap = "silence"                   # programme mode: nothing between blocks
        r["expect"], r["filling"] = None, True
        if gap == "silence":
            self.station.tune(None)           # (let go of the last block's station or album)
            if self.pause:
                self.pause()
                r["paused"] = True
            return
        if isinstance(gap, dict):
            try:
                r["expect"] = self._play_block({"name": "In the gap", "items": [gap], "order": "inorder"})
                r["filling"] = False
                return
            except (ValueError, LookupError) as e:
                log.warning("programme %s: the gap filler can't play (%s): the show", r["name"], e)
        self.station.tune(None)

    def _start_block(self, i: int, due: datetime | None = None) -> None:
        r = self.run
        self._drop_queued()
        prog, now = r["prog"], self.now()
        b = prog["blocks"][i]
        r["i"], r["waiting"], r["block_started"], r["filling"] = i, False, now, False
        if r.pop("paused", False) and self.wake:
            self.wake()                        # (a silent gap is over)
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
        due = due if due is not None and due > now else now
        self._instants(b, due)
        if self._is_moment(b):
            r["expect"], r["ends"] = None, due      # a moment: said or done at its time, then straight on
            log.info("programme %s: moment %s", r["name"], b["name"])
            return
        if not [it for it in b["items"] if it["kind"] not in INSTANT]:
            self._fill_gap()                         # an empty block: the gap's choice, for its time
            return
        try:
            r["expect"] = self._play_block(b)
        except (ValueError, LookupError) as e:          # e.g. a station gone: skip it
            log.warning("programme %s: block %s can't play (%s): skipped", r["name"], b["name"], e)
            r["expect"], r["ends"] = None, now
        log.info("programme %s: block %d/%d %s until %s", r["name"], i + 1, len(prog["blocks"]), b["name"],
                 r["ends"].strftime("%H:%M") if r["ends"] else "it ends")

    @staticmethod
    def _has_end(b: dict) -> bool:
        return any(it["kind"] not in ("show", "list", "station") + INSTANT for it in b["items"])

    def _instants(self, b: dict, due: datetime | None = None) -> None:
        """A block's messages, jingles and actions, at its time (due; now if None).
        The pips and the spoken time go together as one time signal, on the minute."""
        now = self.now()
        due = due or now
        acts = {it["action"] for it in b["items"] if it["kind"] == "action"}
        if acts & {"time", "pips"} and self.time_signal:
            try:
                self.time_signal(due, "pips" in acts, "time" in acts)
            except Exception:
                log.exception("programme: the time signal didn't happen")
        def later(fn, *a):
            wait = (due - now).total_seconds()
            if wait > 0.5:
                t = threading.Timer(wait, fn, a)
                t.daemon = True
                t.start()
            else:
                fn(*a)
        for it in b["items"]:
            try:
                if it["kind"] == "message" and self.say:
                    later(self.say, it["text"])
                elif it["kind"] == "jingle" and self.jingle:
                    later(self.jingle, it["path"])
                elif it["kind"] == "action" and self.action and it["action"] not in ("time", "pips"):
                    later(self.action, it["action"])
                elif it["kind"] == "action" and it["action"] == "time" and not self.time_signal and self.action:
                    later(self.action, "time")          # (no time signal hooked up: the button's way)
            except Exception:
                log.exception("programme: %s didn't happen", it)

    def _play_block(self, b: dict):
        """Start a block on the station; returns what to watch for its end (and
        for something else being chosen): ("source", dict) or ("run", name) or None."""
        st, items = self.station, [it for it in b["items"] if it["kind"] not in INSTANT]
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
        else:                                 # (an old artist one: its one-artist theme)
            st.set_profile(st.theme_for_artist(show["artist"]) if show.get("artist") else None)
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
        if exp is None:                        # the show or silence filling a gap: anything chosen wins
            return bool(self.run.get("filling")) and st.source is not None
        kind, what = exp
        if kind == "source":
            return st.source is not None and st.source is not what
        if kind == "run":
            pl = st.playlist_status()
            return (st.source is not None and st.source["kind"] != "playlist") or (pl is not None and pl["name"] != what)
        return st.source is not None

    def _finish(self) -> None:
        r = self.run
        then = r["prog"]["then"]
        log.info("programme %s: over (%s)", r["name"], then)
        if then == "repeat":
            self._start_block(0)
            return
        if then == "chain":
            nxt = find(self.programmes, r["prog"].get("chain"))
            if nxt is not None and nxt["blocks"]:
                self._drop_queued()
                paused = r.get("paused")
                self.play(nxt["name"])        # (itself: the same as starting again)
                if paused and self.wake:
                    self.wake()
                return
            log.warning("programme %s: no programme %s to play next: back to the show", r["name"], r["prog"].get("chain"))
            then = "show"
        if self.quiet and then in ("show", "keep"):
            then = "stop"                     # programme mode: quiet again afterwards
        if then == "keep":                    # leave whatever's playing as it is
            self.run = None
            return
        self._drop_queued()
        paused = r.get("paused")
        self.run = None
        if self.station.source is not None:
            self.station.tune(None)
        if then == "sleep" and self.sleep:
            self.sleep()
        elif then == "stop" and self.pause:
            self.pause()
        elif paused and self.wake:
            self.wake()                        # (it was silent in a gap: back to sound)
