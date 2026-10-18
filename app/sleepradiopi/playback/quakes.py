"""Earthquakes: the world's from the USGS, the UK's from the British Geological Survey.

Two feeds, both free and with no key, fetched now and then and kept (as the
sun's figures and the forecast are: conditions.Kept):

  * The USGS's summary of the last day: every earthquake of magnitude 4.5
    and over, anywhere -- where, how strong, when, and whether it set off a
    tsunami warning.
  * The BGS's recent UK earthquakes: small ones, mostly (magnitude 1 or 2,
    in Scotland, Wales, the old coalfields), so the latest is worth showing
    whatever its size.

The desktop's "The world outside" has a tile for each (conditions.outside()),
and the Earthquakes window lists them, with how far each is from the radio's
locator. With ALERTS on (conditions.py), the DJ says a fresh one in the next
gap: magnitude WORLD_ALERT and over anywhere, UK_ALERT and over in the UK, or
UK_NEAR_ALERT within UK_NEAR_KM of here -- once, while it's news (ALERT_FRESH_S).
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept

WORLD_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
UK_URL = "https://quakes.bgs.ac.uk/feeds/MhSeismology.xml"
EVERY_S = 900.0              # (the USGS's summaries change every minute; a quarter of an hour is plenty here)
OLD_S = 6 * 3600.0           # what was had this long ago isn't shown as now
WORLD_ALERT = 6.5            # the DJ says one this big anywhere...
UK_ALERT = 3.0               # ...this big in the UK (one a year or so)...
UK_NEAR_ALERT, UK_NEAR_KM = 2.0, 100   # ...or this big this near
ALERT_FRESH_S = 3 * 3600.0   # ...while it's this new: an old one found at start-up isn't news
STRONG, MAJOR = 6.0, 7.0     # the tile: amber from this, red from this


def _json(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _xml(url: str, timeout: float = 12.0) -> ET.Element:
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


def km_from_home(lat: float, lon: float, home: str | None = None) -> int | None:
    """How far a place is from the radio's locator, in km (None with no locator)."""
    home = ft8.HOME if home is None else home
    try:
        hlat, hlon = ft8.locate(home)
    except (ValueError, IndexError, TypeError, AttributeError):
        return None
    p1, p2 = math.radians(hlat), math.radians(lat)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - hlon) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(a)))


def _place(s: str) -> str:
    """"GLENDARUEL,ARGYLL/BUTE" -> "Glendaruel, Argyll/Bute" (the BGS writes them in capitals)."""
    return ", ".join(p.strip().title().replace("And ", "and ") for p in s.split(",") if p.strip())


def world(feed: dict) -> list[dict]:
    """The USGS's GeoJSON summary -> [{"id", "mag", "place", "t", "lat", "lon", "url", "tsunami"}], the biggest first."""
    out = []
    for f in feed.get("features") or []:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if p.get("mag") is None or p.get("time") is None or len(g) < 2 or p.get("type", "earthquake") != "earthquake":
            continue
        out.append({"id": f.get("id") or p.get("code"), "mag": round(float(p["mag"]), 1), "place": p.get("place") or "somewhere at sea",
                    "t": p["time"] / 1000, "lat": g[1], "lon": g[0], "url": p.get("url"), "tsunami": bool(p.get("tsunami"))})
    return sorted(out, key=lambda q: (-q["mag"], -q["t"]))


def uk(root: ET.Element) -> list[dict]:
    """The BGS's RSS -> [{"id", "mag", "place", "t", "lat", "lon", "url"}], the latest first."""
    geo = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"
    out = []
    for item in root.iter("item"):
        desc = item.findtext("description") or ""
        bits = dict((k.strip(), v.strip()) for k, _, v in (b.partition(":") for b in desc.split(";")) if v)
        try:
            when = parsedate_to_datetime(item.findtext("pubDate")).replace(tzinfo=timezone.utc)   # (the BGS gives UTC)
            mag = float(bits["Magnitude"])
            lat, lon = float(item.findtext(geo + "lat")), float(item.findtext(geo + "long"))
        except (TypeError, ValueError, KeyError):
            continue
        link = item.findtext("link") or ""
        out.append({"id": "bgs:" + (link.rsplit("/", 1)[-1].split(".")[0] or str(when.timestamp())), "mag": round(mag, 1),
                    "place": _place(bits.get("Location", "")) or "the UK", "t": when.timestamp(), "lat": lat, "lon": lon, "url": link or None})
    return sorted(out, key=lambda q: -q["t"])


def when_said(t: float, now: float) -> str:
    """A time as the DJ says it: "at 14:05" today, "on Tuesday" this week, "on 3 October" before."""
    at, today = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    days = (today.date() - at.date()).days
    return f"at {at:%H:%M}" if days == 0 else "yesterday" if days == 1 else f"on {at:%A}" if days < 7 else f"on {at.day} {at:%B}"


def said(q: dict, now: float, uk_one: bool = False) -> str:
    far = km_from_home(q["lat"], q["lon"])
    where = f"at {q['place']}" if uk_one else q["place"]
    return (f"Magnitude {q['mag']:g}, {where}, {when_said(q['t'], now)}" + (f", {far:,} km away" if far is not None else "")
            + (". A tsunami warning was issued" if q.get("tsunami") else "") + ".")


def _on(lead: str, words: str) -> str:
    """"An earthquake: " + "Magnitude 6.8, ..." -> "An earthquake: magnitude 6.8, ..."."""
    return lead + words[0].lower() + words[1:]


class Quakes(Kept):
    """{"world": world(), "uk": uk(), "t"}: either may be missing if its site couldn't be reached (not both)."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the earthquakes"

    def __init__(self, get_json: Callable = _json, get_xml: Callable = _xml, clock: Callable[[], float] = time.time) -> None:
        super().__init__(get_json, clock)
        self._get_xml = get_xml

    def _fetch(self) -> dict:
        had = {}
        try:
            had["world"] = world(self._get(WORLD_URL))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            had["uk"] = uk(self._get_xml(UK_URL))
        except (OSError, ValueError, TypeError, AttributeError, ET.ParseError):
            pass
        if not had:
            raise OSError("neither the USGS nor the BGS could be reached")
        return had

    def _says(self, had: dict) -> str:
        now = self._clock()
        parts = []
        if had.get("world"):
            parts.append(_on("The largest earthquake in the last day: ", said(had["world"][0], now)))
        if had.get("uk"):
            parts.append(_on("In the UK, the latest: ", said(had["uk"][0], now, True)))
        return " ".join(parts)


quakes = Quakes()


def alerts(had: dict | None, now: float | None = None) -> list[tuple[str, int, str, str]]:
    """Fresh earthquakes worth the DJ saying (conditions.alerts_now()): [(what, how high, the words, a few words)]."""
    if not had:
        return []
    now = time.time() if now is None else now
    out = []
    for q in had.get("world") or []:
        if q["mag"] >= WORLD_ALERT and now - q["t"] <= ALERT_FRESH_S:
            out.append((f"quake:{q['id']}", round(q["mag"] * 10), _on("An earthquake: ", said(q, now)),
                        f"M{q['mag']:g} earthquake · {q['place'].split(' of ')[-1]}"))
    for q in had.get("uk") or []:
        far = km_from_home(q["lat"], q["lon"])
        if now - q["t"] <= ALERT_FRESH_S and (q["mag"] >= UK_ALERT or (q["mag"] >= UK_NEAR_ALERT and far is not None and far <= UK_NEAR_KM)):
            out.append((f"ukquake:{q['id']}", round(q["mag"] * 10), _on("An earthquake in the UK: ", said(q, now, True)),
                        f"UK earthquake M{q['mag']:g} · {q['place'].split(',')[0]}"))
    return out


def tiles(had: dict | None, alerted: set[str], now: float | None = None) -> list[dict]:
    """The world's and the UK's tiles for "The world outside" (conditions.outside())."""
    if not had:
        return []
    now = time.time() if now is None else now
    out = []
    if "world" in had:
        top = had["world"][0] if had["world"] else None
        out.append({"key": "quakes", "title": "Earthquakes", "value": f"M{top['mag']:g}" if top else "none",
                    "state": "alert" if "quakes" in alerted else "quiet" if not top else "poor" if top["mag"] >= MAJOR else "fair" if top["mag"] >= STRONG else "good",
                    "sub": f"{top['place'].split(' of ')[-1]} · {datetime.fromtimestamp(top['t']):%H:%M}" if top else "none over 4.5 today",
                    "says": (_on("The largest in the last day: ", said(top, now)) if top else "No earthquake of magnitude 4.5 or more in the last day.") + " (USGS)"})
    if had.get("uk"):
        q = had["uk"][0]
        out.append({"key": "ukquakes", "title": "UK earthquakes", "value": f"M{q['mag']:g}", "state": "alert" if "ukquakes" in alerted else "quiet",
                    "sub": f"{q['place'].split(',')[0]} · {datetime.fromtimestamp(q['t']).day} {datetime.fromtimestamp(q['t']):%b}",
                    "says": _on("The latest in the UK: ", said(q, now, True)) + " (British Geological Survey)"})
    return out
