"""Who has been heard, by any of the readers for receivers: one list for them all.

Each reader hands in the stations it reads: FT8, FT4 and WSPR (audio/ft8.py)
from their messages, whose form says who is sending; Morse (audio/morse.py)
and PSK31 / RTTY (audio/keyboard.py) from what they type, through Text. The
list, the map of where they are, the owner's own callsign picked out, and
how the bands are (playback/conditions.py) are then the same for every mode,
and a reader added later only has to hand its stations in.

A station is kept a band at a time and a family of modes at a time: "wsjt"
(FT8, FT4, WSPR: what was one list before), "cw", "psk" (PSK31, PSK63,
QPSK31) and "rtty". For each: when first and last heard, how often, the best
strength the reader gives (its own figure in dB: FT8's isn't Morse's), the
locator if one was given, whether it was calling CQ, and where it was on the
air (Hz) if the page has said where the receiver is tuned (where).

The list is kept on the card (keep_in(); the file the FT8 list always was:
it's read back as it was, as "wsjt"), sparingly: a quarter of an hour apart
at most, when it has changed, and when a reader is switched off.

Text (Morse, PSK31, RTTY): a station is the call after DE -- "CQ CQ DE G4ABC
G4ABC K", "G3XYZ DE G4ABC" -- the one form every operator sends, and the one
that says who is sending rather than who is being called. It has to be read
twice (operators send it twice; a misread call seldom comes out the same
again) before it's believed; after that each sighting counts. "LOC IO94" or
"QRA IO94kn" after it gives its locator. Text comes in pieces, a signal at a
time, and a call can be split between two of them, so a signal's last few
words are kept and a call is taken only once something follows it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

FAMILIES = {"ft8": "wsjt", "ft4": "wsjt", "wspr": "wsjt", "cw": "cw", "psk31": "psk", "psk63": "psk", "qpsk31": "psk", "rtty": "rtty"}
MOST = 1500                  # stations kept (the longest unheard go first)
KEEP_EVERY_S = 900.0         # the list goes to the card this often at most, and only when it has changed
CALL = re.compile(r"^(?:[A-Z0-9]{1,4}/)?(?:[A-Z]{1,2}\d|\d[A-Z])\d{0,2}[A-Z]{1,4}(?:/[A-Z0-9]{1,4})?$")
GRID = re.compile(r"^[A-R]{2}\d{2}$")
_DE = re.compile(r"\bDE\s+([A-Z0-9/]{3,14})(?=\s)")
_LOC = re.compile(r"\b(?:LOC|LOCATOR|QRA|GRID)\W{0,3}([A-R]{2}\d{2}(?:[A-X]{2})?)(?=\W)")
TAIL = 160                   # Text: the characters of a signal kept, for a call split between pieces
TWICE = 2                    # ...the times a call must be read to be believed
TWICE_S = 900.0              # ...within this long
_ENDS = frozenset("K KN BK AR SK 73 TU".split())     # ...and it's calling CQ if CQ comes just before the DE, not before one of these or a call


def family(mode: str) -> str:
    """A reader's mode -> its family ("ft8" -> "wsjt"); ValueError if it isn't one."""
    try:
        return FAMILIES[mode]
    except KeyError:
        raise ValueError(f"no such mode: {mode!r}") from None


class Store:
    """The list. add() a station heard, list() it, forget() some or all of it, keep_in() a file."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._heard: dict[tuple[str | None, str, str], dict] = {}      # (band, call, family) -> the station
        self.where: dict = {}                        # what the page says of the receiver the text readers hear: {"band", "dial", "sideband", "centre"}
        self._file: Path | None = None
        self._changed = False
        self._saved = 0.0

    def add(self, mode: str, band: str | None, call: str, t: float | None = None, snr: float | None = None,
            grid: str | None = None, cq: bool = False, hz: int | None = None) -> dict:
        """A station heard -> it, as now kept (with the locator it gave before, if it gives none now)."""
        fam, t = family(mode), self._clock() if t is None else t
        with self._lock:
            h = self._heard.setdefault((band, call, fam), {"band": band, "call": call, "family": fam, "first": t, "count": 0})
            h.update(mode=mode, last=t, count=h["count"] + 1, cq=bool(cq), **({"grid": grid} if grid else {}), **({"hz": int(hz)} if hz else {}))
            if snr is not None:
                h["snr"] = max(h.get("snr", snr), snr)
            for key in sorted(self._heard, key=lambda k: self._heard[k]["last"])[:max(0, len(self._heard) - MOST)]:
                del self._heard[key]
            self._changed = True
            return dict(h)

    def locate(self, band: str | None, call: str, mode: str, grid: str) -> None:
        """A station already heard gives its locator."""
        with self._lock:
            h = self._heard.get((band, call, family(mode)))
            if h is not None and h.get("grid") != grid:
                h["grid"], self._changed = grid, True

    def get(self, band: str | None, call: str, mode: str) -> dict | None:
        with self._lock:
            h = self._heard.get((band, call, family(mode)))
            return dict(h) if h else None

    def list(self, families=None) -> list[dict]:
        """The stations heard (of those families, or all), the latest heard first."""
        with self._lock:
            out = [dict(h) for h in self._heard.values() if families is None or h["family"] in families]
        return sorted(out, key=lambda h: -h["last"])

    def forget(self, families=None) -> None:
        """Those families' stations (or all) forgotten, on the card too."""
        with self._lock:
            for key in [k for k, h in self._heard.items() if families is None or h["family"] in families]:
                del self._heard[key]
            self._changed = True
        self.save()

    def keep_in(self, file: Path | None) -> None:
        """Where the list is kept from now on, and what's there read back in. One that can't be read is left alone
        until the list is next written. (FT8's list, as it was kept before there was this, comes in as "wsjt".)"""
        self._file = file
        if file is None:
            return
        try:
            heard = {}
            for h in json.loads(file.read_text())["heard"]:
                band, call, fam = h.get("band"), h["call"], h.get("family", "wsjt")
                if not isinstance(call, str) or not (band is None or isinstance(band, str)) or fam not in FAMILIES.values():
                    raise ValueError("a station with no call, a strange band or a strange mode")
                for k in ("first", "last", "count"):
                    h[k] = float(h[k]) if k != "count" else int(h[k])
                if h.get("snr") is not None:
                    h["snr"] = float(h["snr"])
                h["family"] = fam
                h.setdefault("mode", "ft8" if fam == "wsjt" else fam)
                heard[(band, call, fam)] = h
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("heard: the list in %s can't be read (%s): beginning again", file, e)
            return
        with self._lock:
            self._heard = {**heard, **self._heard}
            self._changed = False

    def tick(self) -> None:
        """Every so often, from a reader that's on: the list written to the card if it has changed and the last
        writing was KEEP_EVERY_S ago (a restart loses that much at worst; the card isn't worn by every station)."""
        if self._changed and self._clock() - self._saved >= KEEP_EVERY_S:
            self.save()

    def save(self) -> None:
        """The list written to the card, if it has changed and there's somewhere to keep it."""
        with self._lock:
            if self._file is None or not self._changed:
                return
            text = json.dumps({"heard": list(self._heard.values())}, separators=(",", ":"))
            self._changed, self._saved = False, self._clock()
        try:
            write_atomic(self._file, text)
        except OSError as e:
            log.warning("heard: the list wasn't kept (%s)", e)


def on_air(where: dict, pitch: float | None) -> int | None:
    """Where a signal heard at this pitch is on the air, from where the page says the receiver is tuned; None if
    it hasn't said."""
    dial = where.get("dial")
    if not dial or pitch is None:
        return None
    side = where.get("sideband")
    if side == "lsb":
        return int(round(dial - pitch))
    if side == "cw":
        return int(round(dial + pitch - (where.get("centre") or 0)))
    return int(round(dial + pitch))


class Text:
    """Stations out of what a text reader reads (see the top): take() each piece as it comes, a signal at a time."""

    def __init__(self, mode: str, store: Store) -> None:
        family(mode)
        self.mode, self.store = mode, store
        self._tails: dict = {}                       # signal -> its last few characters
        self._last: dict = {}                        # signal -> the last call read on it (for a locator after it)
        self._seen: dict[tuple[str | None, str], list[float]] = {}     # (band, call) -> when read, not yet believed

    def take(self, signal, text: str, pitch: float | None = None, snr: float | None = None, mode: str | None = None) -> list[dict]:
        """A piece of what a signal typed (signal: anything that tells it from the others, e.g. its pitch rounded)
        -> the stations it made heard (as kept)."""
        old = self._tails.get(signal, "")
        whole = old + str(text).upper()
        new_from, out = len(old), []
        where, now = self.store.where, self.store._clock()
        band = where.get("band")
        for m in _DE.finditer(whole):
            if m.end(1) < new_from:
                continue                             # (taken before: what ended it was in the last piece)
            call = m.group(1)
            if not CALL.match(call) or GRID.match(call):
                continue
            self._last[signal] = call
            known = self.store.get(band, call, mode or self.mode)
            if known is None:
                seen = [t for t in self._seen.get((band, call), []) if now - t < TWICE_S] + [now]
                self._seen[(band, call)] = seen
                if len(seen) < TWICE:
                    continue
                del self._seen[(band, call)]
            cq = False
            for w in reversed(whole[:m.start()].split()[-6:]):
                if w == "CQ":
                    cq = True
                    break
                if w in _ENDS or CALL.match(w):
                    break
            out.append(self.store.add(mode or self.mode, band, call, now, snr, None, cq, on_air(where, pitch)))
        for m in _LOC.finditer(whole):
            if m.end(1) >= new_from and self._last.get(signal):
                g = m.group(1)
                self.store.locate(band, self._last[signal], mode or self.mode, g[:4] + g[4:].lower())
        self._tails[signal] = whole[-TAIL:]
        if len(self._seen) > 400:                    # (misread calls that never came again)
            self._seen = {k: v for k, v in self._seen.items() if now - v[-1] < TWICE_S}
        return out

    def clear(self) -> None:
        self._tails.clear()
        self._last.clear()


store = Store()
