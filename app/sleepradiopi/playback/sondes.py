"""Radiosondes heard, from the receiver's background service on the local network.

The receiver Pi's background service (receiver/sleepradio_background.py, the one the 433 MHz window asks: ONE address for
both, the setting background_url) runs radiosonde_auto_rx in the hours around the weather balloons' launches (about 11:15 and
23:15 UTC) and serves what it decodes as JSON: <address>/sondes (each flight: position, height, climb rate, speed, heading,
temperature... and a thinned track) and <address>/status (what the dongle is doing, and the sonde hours).

Sondes is a conditions.Kept like the 433 MHz one (LAN: no light for it, no file: nothing is written to the card). The service
only knows a flight while it has heard it, so the radio keeps the last few flights itself, in memory, with each one's track
and highest point, and the tile and window exist only while the service answers. Distances are from the aerial (the aircraft
receiver's own place if it gave one, else the middle of the radio's locator square).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sleepradiopi.audio import ft8
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
STATE_WORDS = {"climbing": "climbing", "falling": "descending", "steady": "steady", "landed": "landed (last heard)", "lost": "lost (last heard)"}


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


class Sondes(Kept):
    """{"job", "receiver_busy", "windows", "running", "flights": [newest first], "aerial": [lat, lon] | None, "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the radiosonde decoder"
    LAN = True

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time, address: Callable[[], str] = lambda: radio433.ADDRESS) -> None:
        super().__init__(get, clock)
        self._address = address
        self._mem = threading.Lock()
        self._flights: dict[str, dict] = {}           # id -> what is known of it (memory only, never the card)

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
                if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not all(isinstance(f.get(k), (int, float)) for k in ("lat", "lon", "alt")):
                    continue
                e = self._flights.setdefault(f["id"], {"id": f["id"][:20], "first": now, "track": [], "max_alt": f["alt"]})
                latest = {k: f.get(k) for k in ("type", "freq", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "temp", "humidity", "pressure", "batt", "snr", "state")}
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
            for k in sorted(self._flights, key=lambda k: self._flights[k]["heard"])[:-KEEP]:
                del self._flights[k]
            flights = [{**{k: v for k, v in e.items() if k != "heard"}, "last_heard": e["heard"]} for e in self._flights.values()]
        flights.sort(key=lambda e: e["last_heard"], reverse=True)
        at = aerial()
        for e in flights:
            e["ago_s"] = max(0, round(now - e["last_heard"]))
            e["state"] = judge({**e, "state": e.get("state")})
            if at:
                nm, deg = distance(at[0], at[1], e["lat"], e["lon"])
                e["km"], e["deg"] = round(nm * 1.852, 1), round(deg)
        return {"job": status.get("job") if isinstance(status.get("job"), str) else None, "receiver_busy": bool(status.get("receiver_busy")),
                "windows": [w for w in status.get("sonde_windows_utc") or [] if isinstance(w, str)][:6], "running": bool(data.get("running")),
                "flights": flights, "aerial": list(at) if at else None}

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
        gone = round(self._clock() - had["t"])         # (as of now: what was heard 15 s ago was heard that much longer ago)
        flights = []
        for e in had["flights"]:
            e = {**e, "ago_s": e["ago_s"] + gone}
            e["state"] = judge(e)
            flights.append(e)
        return {**had, "flights": flights}

    def forget(self) -> None:
        with self._lock:
            self._had, self._tried = None, 0.0
        with self._mem:
            self._flights.clear()

    def _says(self, had: dict) -> str:
        live = [f for f in had["flights"] if f["ago_s"] < ACTIVE_S]
        if live:
            f = live[0]
            return f"{f.get('type') or 'A radiosonde'} {f['id']} is {STATE_WORDS.get(f['state'], f['state'])} at {f['alt'] / 1000:.1f} km" + (f", {f['km']:g} km from you." if "km" in f else ".")
        return radio433.state_word(had) + ". No radiosonde is being heard."


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
