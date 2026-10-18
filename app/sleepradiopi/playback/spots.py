"""My signal: who has heard the owner's callsign lately, from PSK Reporter.

When you transmit FT8, FT4, WSPR, PSK and the like, the stations that decode
you report it to PSK Reporter, and it answers anyone who asks: who heard a
callsign, where, on what band, how well. Spots asks it about the owner's
callsign (the one kept for the rooms: ft8.own_call()) over the last day,
now and then (EVERY_S: PSK Reporter asks for five minutes at least between
queries), and keeps it (conditions.Kept). Nothing is asked with no callsign.

The desktop's "The world outside" has a My signal tile (how many heard you,
the farthest); the My signal window lists them, with a link to PSK
Reporter's map. Nothing is said aloud.
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept

URL = "https://retrieve.pskreporter.info/query?senderCallsign={}&flowStartSeconds=-86400&rronly=1&appcontact={}"
CONTACT = "https://github.com/dylan7474/SleepRadioPi-OS"      # (who's asking, as PSK Reporter asks: the project, not the owner)
MAP = "https://pskreporter.info/pskmap.html?preset&callsign={}&timerange=86400&txrx=tx"
EVERY_S = 900.0
OLD_S = 6 * 3600.0
MOST = 200                   # spots kept
LATELY_S = 3600.0            # heard this lately: the tile's green
BANDS = ((1.8, 2.0, "160 m"), (3.5, 4.0, "80 m"), (5.25, 5.45, "60 m"), (7.0, 7.3, "40 m"), (10.1, 10.15, "30 m"), (14.0, 14.35, "20 m"),
         (18.068, 18.168, "17 m"), (21.0, 21.45, "15 m"), (24.89, 24.99, "12 m"), (28.0, 29.7, "10 m"), (50.0, 54.0, "6 m"),
         (70.0, 70.5, "4 m"), (144.0, 146.0, "2 m"), (430.0, 440.0, "70 cm"))


def band(hz: float | None) -> str | None:
    """14074000 -> "20 m"."""
    if not hz:
        return None
    mhz = hz / 1e6
    return next((name for lo, hi, name in BANDS if lo <= mhz <= hi), f"{mhz:.3f} MHz")


def _get_xml(url: str, timeout: float = 25.0) -> ET.Element:
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


def read(root: ET.Element, home: str | None = None) -> list[dict]:
    """PSK Reporter's answer -> [{"call", "grid", "km"?, "hz", "band", "mode", "snr", "t", "country"}], the latest first:
    the latest spot from each station."""
    home = ft8.HOME if home is None else home
    by: dict[str, dict] = {}
    for r in root.iter("receptionReport"):
        try:
            t = float(r.get("flowStartSeconds"))
        except (TypeError, ValueError):
            continue
        call = (r.get("receiverCallsign") or "").upper()
        if not call or (call in by and by[call]["t"] >= t):
            continue
        grid = (r.get("receiverLocator") or "").strip() or None
        hz = float(r.get("frequency")) if (r.get("frequency") or "").isdigit() else None
        spot = {"call": call, "grid": grid, "hz": hz, "band": band(hz), "mode": r.get("mode"), "t": t, "country": r.get("receiverDXCC"),
                "snr": int(r.get("sNR")) if (r.get("sNR") or "").lstrip("-").isdigit() else None}
        if home and grid:
            try:
                spot["km"] = ft8.path(home, grid[:6])[0]
            except (ValueError, IndexError, TypeError):
                pass
        by[call] = spot
    return sorted(by.values(), key=lambda s: -s["t"])[:MOST]


class Spots(Kept):
    """{"call", "spots": read(), "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "PSK Reporter's spots"

    def __init__(self, get: Callable = _get_xml, clock: Callable[[], float] = time.time, call: Callable = ft8.own_call) -> None:
        super().__init__(get, clock)
        self._call = call

    def _fetch(self) -> dict:
        call = (self._call() or "").upper()
        if not call:
            raise ValueError("no callsign set")
        return {"call": call, "spots": read(self._get(URL.format(urllib.parse.quote(call), urllib.parse.quote(CONTACT, safe=""))))}

    def status(self) -> dict | None:
        had = super().status()
        if had and had.get("call") != (self._call() or "").upper():      # (the callsign's changed: those were someone else's)
            return None
        return had

    def _says(self, had: dict) -> str:
        return said(had)


spots = Spots()


def said(had: dict) -> str:
    """Who heard the callsign, in a sentence."""
    sp = had.get("spots") or []
    if not sp:
        return f"{had['call']} hasn't been heard in the last day."
    far = max(sp, key=lambda s: s.get("km") or 0)
    return (f"{had['call']} was heard by {len(sp)} station{'' if len(sp) == 1 else 's'} in the last day"
            + (f", the farthest {far['call']} at {far['km']:,} km ({far['band']}, {far['mode']})" if far.get("km") else "") + ".")


def tile(had: dict | None, now: float | None = None) -> list[dict]:
    """The My signal tile for "The world outside" (conditions.outside())."""
    if not had:
        return []
    now = time.time() if now is None else now
    sp = had.get("spots") or []
    far = max(sp, key=lambda s: s.get("km") or 0) if sp else None
    return [{"key": "signal", "title": "My signal", "value": f"{len(sp)} heard" if sp else "none",
             "state": "quiet" if not sp else "good" if now - sp[0]["t"] <= LATELY_S else "fair",
             "sub": (f"farthest {far['km']:,} km · {far['band']}" if far and far.get("km") else f"{had['call']} · last day") if sp else "not heard today",
             "says": said(had) + " (PSK Reporter)"}]
