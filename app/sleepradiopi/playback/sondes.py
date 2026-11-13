"""Radiosondes heard, from the receiver's background service on the local network.

The receiver Pi's background service (receiver/sleepradio_background.py, the one the 433 MHz window asks: ONE address for
both, the setting background_url) runs radiosonde_auto_rx in the hours around the weather balloons' launches (about 11:15 and
23:15 UTC) and serves what it decodes as JSON: <address>/sondes (each flight: position, height, climb rate, speed, heading,
temperature... and a thinned track), <address>/sondes/series?id= (one flight's readings over time: a row every 5 s or so) and
<address>/status (what the dongle is doing, and the sonde hours).

Sondes is a conditions.Kept like the 433 MHz one (LAN: no light for it, no file: nothing is written to the card). The service
only knows a flight while it has heard it, so the radio keeps the last few flights itself, in memory, with each one's track
and highest point and its readings over time (the series: asked for a few times a flight, not every look), and the tile
and window exist only while the service answers. Distances are from the aerial (the aircraft
receiver's own place if it gave one, else the middle of the radio's locator square).
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback import radio433
from sleepradiopi.playback.aircraft import _get, base, distance
from sleepradiopi.playback.conditions import Kept

log = logging.getLogger(__name__)

EVERY_S = 15.0
OLD_S = 120.0                # a picture this old is no picture: the tile goes
ACTIVE_S = 120               # a flight heard this recently is on the air now
KEEP = 8                     # flights kept (the newest), each with its track
TRACK_MAX = 400              # points of a track kept
LANDED_M = 1500              # a flight last heard below this, falling, and then silent, has landed (or is as good as: the signal goes when it is low)
LANDED_S = 300               # ...silent for this long
M_TO_FT = 3.28084
MAX_PINS = 10                # flights kept over a restart (a file of a few tens of KB: one write per pin, never a steady stream)
FORMAT = "sleepradiopi-sondes"
VERSION = 2                  # 2: a flight may have its series (1: position, height and the last readings only; still read)
SERIES_KEEP = 600            # rows of a flight's series the radio keeps (thinned evenly: one every 15 s or so of a 2.5 h flight)
SERIES_EVERY_S = 300.0       # a flight still being heard: its series is asked for again at most this often (and once more when it goes silent)
SERIES_COLUMNS = ("t", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "temp", "humidity", "pressure", "batt", "snr")
HIDDEN = ("heard", "series", "series_at", "series_of")      # (not in the window's list: the series is asked for on its own, /api/sondes/profile)
FIELDS = ("type", "freq", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "temp", "humidity", "pressure", "batt", "snr", "state")
STATE_WORDS = {"climbing": "climbing", "falling": "descending", "steady": "steady", "landed": "landed (last heard)", "lost": "lost (last heard)"}


def sonde_word(had: dict) -> str:
    """What the dongle is doing, said for THIS window (the 433 MHz one says it its own way): the sonde job, or why it isn't."""
    if had.get("job") == "sondes":
        return "Listening for radiosondes"
    if had.get("job") == "scan433":
        return "Not listening for radiosondes now: the dongle is scanning 433 MHz sensors"
    return "Not listening for radiosondes now: the VHF receiver is in use" if had.get("receiver_busy") else "Not listening for radiosondes now (starting again)"


def aerial() -> tuple[float, float] | None:
    """Where the aerial is: the aircraft receiver's own place if it said, else the middle of the radio's locator square."""
    from sleepradiopi.playback import aircraft
    had = aircraft.aircraft.status(False)
    if had and had.get("at"):
        return tuple(had["at"])
    if ft8.HOME:
        try:
            return tuple(ft8.locate(ft8.HOME))
        except (ValueError, IndexError, TypeError, AttributeError):
            return None
    return None


def next_window(windows: list[str], minute: int) -> tuple[str, str, bool] | None:
    """The sonde hours ("22:45-01:30", UTC) and the minute of the day now -> (its start, its end, whether it is on now): the one on now, else the next."""
    spans = []
    for w in windows:
        try:
            a, b = w.split("-")
            (h1, m1), (h2, m2) = a.split(":"), b.split(":")
            spans.append((int(h1) * 60 + int(m1), int(h2) * 60 + int(m2), a, b))
        except ValueError:
            continue
    for start, end, a, b in spans:
        if (start <= minute < end) if start <= end else (minute >= start or minute < end):
            return a, b, True
    if not spans:
        return None
    nxt = min(spans, key=lambda s: (s[0] - minute) % 1440)
    return nxt[2], nxt[3], False


def judge(f: dict) -> str:
    """climbing / falling / steady while it is being heard; landed or lost (last heard) once it is silent."""
    if f["ago_s"] < ACTIVE_S:
        return f["state"] if f.get("state") in ("climbing", "falling", "steady") else "steady"
    low = f["alt"] < LANDED_M
    return "landed" if f["ago_s"] >= LANDED_S and low and f.get("max_alt", 0) - f["alt"] > 1000 else "lost" if f["ago_s"] >= LANDED_S else f.get("state", "steady")


class BadFlight(ValueError):
    """The file isn't a radiosonde file, or a flight in it is wrong."""


def _num(v, lo=-1e7, hi=1e7) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def thin(rows: list, most: int = SERIES_KEEP) -> list:
    """At most `most` rows, evenly spread, the first and the last kept."""
    if len(rows) <= most:
        return rows
    step = (len(rows) - 1) / (most - 1)
    return [rows[round(i * step)] for i in range(most)]


def series_from(data, most: int = SERIES_KEEP) -> list[list] | None:
    """{"columns": [...], "rows": [[...]]} (the service's, or a file's) -> rows in SERIES_COLUMNS order, checked and thinned; None if it isn't one.
    A column it doesn't have is None; a row without a time and a place is left out."""
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list) or not isinstance(data.get("rows"), list):
        return None
    cols = data["columns"]
    at = [cols.index(c) if c in cols else None for c in SERIES_COLUMNS]
    out = []
    for r in data["rows"][:20000]:
        if not isinstance(r, list):
            continue
        row = [r[i] if i is not None and i < len(r) and (_num(r[i], 0, 1e11) if c == "t" else _num(r[i])) else None for c, i in zip(SERIES_COLUMNS, at)]
        if None in row[:4] or not (-90 <= row[1] <= 90 and -180 <= row[2] <= 180):
            continue
        out.append(row)
    out.sort(key=lambda row: row[0])
    return thin(out, most)


def clean(f) -> dict:
    """One flight from a file, checked: only the fields a flight has, each the right kind (BadFlight if not)."""
    if not isinstance(f, dict):
        raise BadFlight("a flight must be an object")
    i = f.get("id")
    if not isinstance(i, str) or not re.fullmatch(r"[\w.-]{1,20}", i):
        raise BadFlight("a flight needs an id (letters, numbers, . - _)")
    out = {"id": i}
    for k in ("lat", "lon", "alt", "max_alt", "first", "last_heard"):
        if not (_num(f.get(k), 0, 1e11) if k in ("first", "last_heard") else _num(f.get(k))):
            raise BadFlight(f"{i}: {k} is missing or not a number")
        out[k] = f[k]
    if not (-90 <= out["lat"] <= 90 and -180 <= out["lon"] <= 180):
        raise BadFlight(f"{i}: the position is off the Earth")
    for k in FIELDS:
        v = f.get(k)
        if k in ("type", "freq", "state"):
            out[k] = v[:30] if isinstance(v, str) else None
        elif k not in out:
            out[k] = v if _num(v) else None
    track = f.get("track")
    if not isinstance(track, list) or len(track) > TRACK_MAX * 2:
        raise BadFlight(f"{i}: the track is missing or too long")
    out["track"] = []
    for p in track:
        if not (isinstance(p, list) and len(p) >= 3 and all(_num(v) for v in p[:3]) and abs(p[0]) <= 90 and abs(p[1]) <= 180):
            raise BadFlight(f"{i}: a point of the track is wrong")
        out["track"].append([p[0], p[1], p[2]])
    while len(out["track"]) > TRACK_MAX:
        del out["track"][1::2]
    if "series" in f:
        rows = series_from(f["series"])
        if rows is None:
            raise BadFlight(f"{i}: the readings over time are wrong")
        if rows:
            out["series"] = rows
    return out


def parse(data) -> list[dict]:
    """A radiosonde file's flights, checked (BadFlight if it isn't one, or any flight in it is wrong)."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BadFlight("this isn't a Sleep Radio radiosonde file")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise BadFlight("this file is from a newer version of the radio")
    flights = data.get("flights")
    if not isinstance(flights, list) or not flights or len(flights) > 50:
        raise BadFlight("the file has no flights in it (or too many)")
    return [clean(f) for f in flights]


class Sondes(Kept):
    """{"job", "receiver_busy", "windows", "running", "flights": [newest first], "aerial": [lat, lon] | None, "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the radiosonde decoder"
    LAN = True

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time, address: Callable[[], str] = lambda: radio433.ADDRESS) -> None:
        super().__init__(get, clock)
        self._address = address
        self._mem = threading.Lock()
        self._flights: dict[str, dict] = {}           # id -> what is known of it (memory; only a pinned flight is also in the pins file)
        self._pins: Path | None = None
        self._deleted: set[str] = set()               # ids deleted here: not taken back from the receiver, which still has them (memory: a restart forgets this)
        self._saved: dict[str, tuple] = {}            # pinned id -> (track points, series rows) in the file (a pinned flight still being heard is written again once it is silent)

    def _fetch(self) -> dict:
        url = base(self._address())
        if not url:
            raise ValueError("no address")
        data, status = self._get(url + "/sondes"), self._get(url + "/status")
        if not isinstance(data.get("flights"), list):
            raise ValueError("no flight list")
        now = self._clock()
        with self._mem:
            for f in data["flights"]:
                if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not all(isinstance(f.get(k), (int, float)) for k in ("lat", "lon", "alt")) or f["id"] in self._deleted:
                    continue
                e = self._flights.setdefault(f["id"], {"id": f["id"][:20], "first": now, "track": [], "max_alt": f["alt"]})
                latest = {k: f.get(k) for k in FIELDS}
                e.update(latest)
                e["heard"] = now - float(f.get("ago_s") or 0)
                track = [p for p in (f.get("track") or []) if isinstance(p, list) and len(p) >= 3 and all(isinstance(v, (int, float)) for v in p[:3])]
                e["max_alt"] = max([e["max_alt"], f["alt"]] + [p[2] for p in track])       # (the highest it has been, from the service's track too: the radio may have just started)
                if len(track) >= len(e["track"]):
                    e["track"] = track
                elif not e["track"] or e["track"][-1][:2] != [f["lat"], f["lon"]]:
                    e["track"].append([f["lat"], f["lon"], f["alt"]])
                while len(e["track"]) > TRACK_MAX:
                    del e["track"][1::2]
            self._trim()
            wanted = [(f["id"], f.get("series_rows")) for f in data["flights"] if isinstance(f, dict) and self._series_due(f, now)]
        for ident, n in wanted:                                     # (asked outside the lock: the network is slow)
            self._fetch_series(url, ident, n, now)
        with self._mem:
            self._resave(now)
        flights = self._view(now)
        at = aerial()
        for e in flights:
            if at:
                nm, deg = distance(at[0], at[1], e["lat"], e["lon"])
                e["km"], e["deg"] = round(nm * 1.852, 1), round(deg)
        return {"job": status.get("job") if isinstance(status.get("job"), str) else None, "receiver_busy": bool(status.get("receiver_busy")),
                "windows": [w for w in status.get("sonde_windows_utc") or [] if isinstance(w, str)][:6], "running": bool(data.get("running")),
                "flights": flights, "aerial": list(at) if at else None}

    def _series_due(self, f: dict, now: float) -> bool:
        """Should this flight's series be asked for now? (the caller holds _mem) When the service has rows the radio hasn't: every
        SERIES_EVERY_S while it is heard, and once more as soon as it has gone silent."""
        e = self._flights.get(f.get("id"))
        n = f.get("series_rows")
        if e is None or not isinstance(n, int) or n <= 0 or n == e.get("series_of"):
            return False
        silent = isinstance(f.get("ago_s"), (int, float)) and f["ago_s"] >= ACTIVE_S
        return now - e.get("series_at", 0.0) >= (60.0 if silent else SERIES_EVERY_S)

    def _fetch_series(self, url: str, ident: str, n: int, now: float) -> None:
        try:
            rows = series_from(self._get(url + "/sondes/series?id=" + quote(ident)))
        except (OSError, ValueError) as e:
            log.info("sondes: %s's readings couldn't be had (%s)", ident, e)
            rows = None
        with self._mem:
            e = self._flights.get(ident)
            if e is None:
                return
            e["series_at"] = now
            if rows is not None:
                e["series_of"] = n
                if len(rows) >= len(e.get("series") or ()) or not e.get("series"):
                    e["series"] = rows

    def profile(self, ident: str) -> dict:
        """One flight's whole story, for the 3D view: its track and its series (BadFlight if there is no such flight)."""
        with self._mem:
            e = self._flights.get(ident)
            if e is None:
                raise BadFlight("there's no such flight")
            return {"id": e["id"], "type": e.get("type"), "max_alt": e["max_alt"], "first": e["first"], "last_heard": e["heard"],
                    "track": [list(p) for p in e["track"]], "columns": list(SERIES_COLUMNS), "rows": [list(r) for r in e.get("series") or ()]}

    # -- flights kept: pinned (over a restart), exported and imported (a file on the PC) --

    def _trim(self) -> None:
        """Only the newest KEEP flights, not counting the pinned ones (the caller holds _mem)."""
        loose = sorted((k for k, e in self._flights.items() if not e.get("pinned")), key=lambda k: self._flights[k]["heard"])
        for k in loose[:-KEEP] if KEEP else loose:
            del self._flights[k]

    def _view(self, now: float) -> list[dict]:
        """Every flight kept, newest first, with how long ago and what it is doing (the caller holds nothing)."""
        with self._mem:
            flights = [{**{k: v for k, v in e.items() if k not in HIDDEN}, "last_heard": e["heard"], "series_rows": len(e.get("series") or ())}
                       for e in self._flights.values()]
        flights.sort(key=lambda e: e["last_heard"], reverse=True)
        for e in flights:
            e["ago_s"] = max(0, round(now - e["last_heard"]))
            e["state"] = judge({**e, "state": e.get("state")})
        return flights

    def kept(self) -> list[dict]:
        """The flights kept, for when the receiver isn't answering (pinned and imported ones are still the radio's own)."""
        now = self._clock()
        flights = self._view(now)
        at = aerial()
        for e in flights:
            if at:
                nm, deg = distance(at[0], at[1], e["lat"], e["lon"])
                e["km"], e["deg"] = round(nm * 1.852, 1), round(deg)
        return flights

    def pins_in(self, file: Path | None) -> None:
        """Where pinned flights are kept over a restart; load them now."""
        self._pins = file
        if file is None:
            return
        try:
            flights = parse(json.loads(file.read_text()))
        except (OSError, ValueError):
            return
        with self._mem:
            for f in flights[:MAX_PINS]:
                self._flights[f["id"]] = {**{k: v for k, v in f.items() if k != "last_heard"}, "heard": f["last_heard"], "pinned": True}
                self._saved[f["id"]] = self._size(f)

    def _write_pins(self) -> None:
        """The pinned flights into the file (the caller holds _mem)."""
        if self._pins is None:
            return
        pinned = [e for e in self._flights.values() if e.get("pinned")]
        try:
            if pinned:
                write_atomic(self._pins, json.dumps(self._document(pinned)))
            else:
                self._pins.unlink(missing_ok=True)
            self._saved = {e["id"]: self._size(e) for e in pinned}
        except OSError as e:
            log.warning("sondes: the pinned flights couldn't be written (%s)", e)

    def _resave(self, now: float) -> None:
        """A pinned flight that was still being heard when pinned has grown since: write it again, once, when it has gone silent (the caller holds _mem)."""
        if any(e.get("pinned") and self._size(e) != self._saved.get(e["id"]) and now - e["heard"] >= ACTIVE_S for e in self._flights.values()):
            self._write_pins()

    @staticmethod
    def _size(e: dict) -> tuple:
        return len(e["track"]), len(e.get("series") or ())

    def _document(self, flights) -> dict:
        out = []
        for e in flights:
            out.append({"id": e["id"], "first": e["first"], "max_alt": e["max_alt"], "last_heard": e["heard"], "track": e["track"], **{k: e.get(k) for k in FIELDS},
                        **({"series": {"columns": list(SERIES_COLUMNS), "rows": e["series"]}} if e.get("series") else {})})
        return {"format": FORMAT, "version": VERSION, "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "flights": out}

    def export(self, ident: str | None = None) -> dict:
        """The file for one flight, or all of them (BadFlight if there is none)."""
        with self._mem:
            chosen = [e for e in sorted(self._flights.values(), key=lambda e: e["heard"], reverse=True) if ident is None or e["id"] == ident]
            if not chosen:
                raise BadFlight("there's no such flight")
            return self._document(chosen)

    def load(self, data) -> list[str]:
        """Take the flights of a file into the radio's memory (an imported one is shown as such; one already here is kept
        unless the file's has more of its track). -> their ids. Nothing is changed if any of it is wrong."""
        flights = parse(data)
        with self._mem:
            for f in flights:
                self._deleted.discard(f["id"])                     # (imported on purpose: it is wanted again)
                had = self._flights.get(f["id"])
                if had and self._size(had)[::-1] >= self._size(f)[::-1]:          # (the one with more readings, then more track, stays)
                    continue
                self._flights[f["id"]] = {**{k: v for k, v in f.items() if k != "last_heard"}, "heard": f["last_heard"], "imported": True,
                                          "pinned": bool(had and had.get("pinned"))}
            self._trim()
            if any(e.get("pinned") for e in self._flights.values()):
                self._write_pins()
        return [f["id"] for f in flights]

    def pin(self, ident: str, on: bool = True) -> None:
        """Keep this flight over a restart (or stop doing so). BadFlight if there is no such flight or too many are pinned."""
        with self._mem:
            e = self._flights.get(ident)
            if e is None:
                raise BadFlight("there's no such flight")
            if on and not e.get("pinned") and sum(1 for x in self._flights.values() if x.get("pinned")) >= MAX_PINS:
                raise BadFlight(f"up to {MAX_PINS} flights can be pinned: unpin one first")
            e["pinned"] = on
            self._write_pins()
            self._trim()

    def delete(self, ident: str) -> None:
        """Forget this flight now, pinned or not (a pinned one leaves the pins file too). The receiver may still be serving it: it isn't
        taken back from it (until the radio restarts). BadFlight if there is no such flight."""
        with self._mem:
            e = self._flights.pop(ident, None)
            if e is None:
                raise BadFlight("there's no such flight")
            self._deleted.add(ident)
            if e.get("pinned"):
                self._write_pins()

    def wanted(self) -> bool:
        return base(self._address()) is not None

    def refresh(self) -> None:
        if self.wanted():
            super().refresh()

    def status(self, refresh: bool = True, old_ok: bool = False) -> dict | None:
        if not self.wanted():
            return None
        had = super().status(refresh, False)
        if had is None:
            return None
        # The flights from memory as they are now, not as they were at the last ask of the receiver (up to half a minute ago): a pin, an
        # unpin or an import shows at once (it didn't: the tick box went back until the next ask), and "ago" is as of now.
        return {**had, "flights": self.kept()}

    def forget(self) -> None:
        with self._lock:
            self._had, self._tried = None, 0.0
        with self._mem:
            self._flights = {k: e for k, e in self._flights.items() if e.get("pinned")}      # (a pinned flight is the radio's own, not the receiver's)

    def _says(self, had: dict) -> str:
        live = [f for f in had["flights"] if f["ago_s"] < ACTIVE_S]
        if live:
            f = live[0]
            return f"{f.get('type') or 'A radiosonde'} {f['id']} is {STATE_WORDS.get(f['state'], f['state'])} at {f['alt'] / 1000:.1f} km" + (f", {f['km']:g} km from you." if "km" in f else ".")
        return sonde_word(had) + ". No radiosonde is being heard."


def tile(had: dict | None, alerted: set[str], minute: int | None = None) -> list[dict]:
    """The Radiosondes tile for "The world outside" (conditions.outside()): there only while the receiver answers."""
    if not had:
        return []
    live = [f for f in had["flights"] if f["ago_s"] < ACTIVE_S]
    if minute is None:
        t = time.gmtime()
        minute = t.tm_hour * 60 + t.tm_min
    nxt = next_window(had.get("windows") or [], minute)
    if live:
        f = live[0]
        sub = f"{f.get('type') or 'sonde'} {f['alt'] / 1000:.1f} km, {STATE_WORDS.get(f['state'], f['state'])}"
    elif had["flights"]:
        f = had["flights"][0]
        sub = f"last: {f.get('type') or 'sonde'} {STATE_WORDS.get(f['state'], f['state'])}"
    elif nxt:
        sub = f"listening until {nxt[1]} UTC" if nxt[2] else f"next listening {nxt[0]} UTC"
    else:
        sub = "none heard"
    return [{"key": "sondes", "title": "Radiosondes", "value": str(len(live)), "state": "good" if live else "quiet", "sub": sub,
             "says": Sondes._says(None, had) + " (your receiver's radiosonde decoder)"}]


sondes = Sondes()
