"""Warnings: the Met Office's severe weather warnings, and the Environment Agency's flood warnings.

Two free feeds with no key, fetched now and then and kept (conditions.Kept):

  * The Met Office's warnings for one of its sixteen regions (an RSS feed
    each): yellow, amber or red, of what (rain, wind, snow, ice, fog,
    thunderstorms, heat), and when it's valid. The region is REGION, or
    the one whose middle is nearest the radio's locator (region_for()).
  * The Environment Agency's flood warnings in force within FLOOD_KM of the
    locator (England only: Scotland, Wales and Northern Ireland have their
    own agencies): a flood alert (be prepared), a flood warning (flooding
    is expected) or a severe flood warning (danger to life).

The desktop's "The world outside" has a Warnings tile, the most serious one
on its face; the Warnings window lists them all. With ALERTS on
(conditions.py), the DJ says a new amber or red warning, or a flood
warning or severe flood warning, once -- and again if it's raised.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept

MET_URL = "https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/{}"
EA_URL = "https://environment.data.gov.uk/flood-monitoring/id/floods?lat={:.3f}&long={:.3f}&dist={}"
EA_PAGE = "https://check-for-flooding.service.gov.uk/target-area/{}"
EVERY_S = 900.0
OLD_S = 6 * 3600.0
FLOOD_KM = 30
REGION = ""                  # the Met Office region chosen ("ne"...), or "" for the one nearest the locator; main.py sets it
# The Met Office's regions: code -> (name, roughly its middle)
REGIONS = {
    "os": ("Orkney & Shetland", 59.6, -2.0), "he": ("Highlands & Eilean Siar", 57.5, -5.0), "gr": ("Grampian", 57.2, -2.6),
    "st": ("Strathclyde", 55.7, -4.7), "ta": ("Central, Tayside & Fife", 56.4, -3.5), "dg": ("Dumfries, Galloway, Lothian & Borders", 55.4, -3.2),
    "ni": ("Northern Ireland", 54.6, -6.7), "wl": ("Wales", 52.3, -3.7), "nw": ("North West England", 54.0, -2.7),
    "ne": ("North East England", 54.8, -1.7), "yh": ("Yorkshire & Humber", 53.9, -1.2), "wm": ("West Midlands", 52.5, -2.2),
    "em": ("East Midlands", 52.9, -0.9), "ee": ("East of England", 52.3, 0.6), "sw": ("South West England", 50.8, -3.6),
    "se": ("London & South East England", 51.2, -0.4),
}
MET_LEVELS = ("yellow", "amber", "red")
FLOOD_KIND = {1: "severe flood warning", 2: "flood warning", 3: "flood alert"}


def _get_text(url: str, timeout: float = 12.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _home() -> tuple[float, float] | None:
    try:
        return ft8.locate(ft8.HOME) if ft8.HOME else None
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def region_for(here: tuple[float, float] | None = None) -> str | None:
    """The Met Office region whose middle is nearest (REGION, if one's chosen); None with neither."""
    if REGION in REGIONS:
        return REGION
    here = here or _home()
    if here is None or not (49 <= here[0] <= 61 and -9 <= here[1] <= 2):      # (outside the UK: none of them)
        return None
    lat, lon = here
    return min(REGIONS, key=lambda r: (REGIONS[r][1] - lat) ** 2 + ((REGIONS[r][2] - lon) * math.cos(math.radians(lat))) ** 2)


def met(root: ET.Element) -> list[dict]:
    """A region's RSS -> [{"id", "level", "what", "valid", "title", "url"}], the most serious first."""
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        m = re.match(r"(yellow|amber|red)\s+warning\s+of\s+(.+?)\s+affecting\b", title, re.I)
        if not m:
            continue
        desc = item.findtext("description") or ""
        valid = re.search(r"valid\s+from\s+(.+?)\s+to\s+(.+?)(?:\s*$|\.|<)", desc, re.I)
        out.append({"id": (item.findtext("guid") or item.findtext("link") or title).strip(), "level": m.group(1).lower(),
                    "what": m.group(2).strip().lower(), "valid": f"{valid.group(1)} to {valid.group(2)}" if valid else None,
                    "title": title, "url": (item.findtext("link") or "").strip() or None})
    return sorted(out, key=lambda w: -MET_LEVELS.index(w["level"]))


def floods(feed: dict) -> list[dict]:
    """The EA's floods -> [{"id", "severity" (1 severe, 2 warning, 3 alert), "kind", "where", "river", "raised", "message", "url"}],
    the most serious first (those no longer in force left out)."""
    out = []
    for f in feed.get("items") or []:
        sev = f.get("severityLevel")
        if sev not in FLOOD_KIND:
            continue
        area = f.get("floodArea") or {}
        aid = f.get("floodAreaID") or area.get("notation") or ""
        out.append({"id": aid, "severity": sev, "kind": FLOOD_KIND[sev], "where": (f.get("description") or "").strip() or "near here",
                    "river": area.get("riverOrSea"), "raised": f.get("timeSeverityChanged") or f.get("timeRaised"),
                    "message": (f.get("message") or "").strip() or None, "url": EA_PAGE.format(aid) if aid else None})
    return sorted(out, key=lambda w: w["severity"])


class Warnings(Kept):
    """{"region", "met": met(), "floods": floods(), "t"}: either list may be missing if its site couldn't be reached."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the warnings"

    def __init__(self, get: Callable = _get_text, clock: Callable[[], float] = time.time) -> None:
        super().__init__(get, clock)

    def _fetch(self) -> dict:
        here, had = _home(), {}
        region = region_for(here)
        if region:
            had["region"] = region
            try:
                had["met"] = met(ET.fromstring(self._get(MET_URL.format(region))))
            except (OSError, ValueError, ET.ParseError):
                pass
        if here:
            try:
                had["floods"] = floods(json.loads(self._get(EA_URL.format(here[0], here[1], FLOOD_KM))))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if "met" not in had and "floods" not in had:
            raise OSError("neither the Met Office nor the Environment Agency could be reached" if here else "no locator set")
        return had

    def _says(self, had: dict) -> str:
        return says(had)


warnings = Warnings()


def _met_said(w: dict, region: str | None) -> str:
    return (f"{'An' if w['level'] == 'amber' else 'A'} {w['level']} warning of {w['what']}" + (f" for {REGIONS[region][0]}" if region in REGIONS else "")
            + (f", valid from {w['valid']}" if w.get("valid") else "") + ".")


def says(had: dict | None) -> str:
    """Everything in force, in sentences."""
    if not had:
        return ""
    parts = [_met_said(w, had.get("region")) for w in had.get("met") or []]
    parts += [f"A {f['kind']}: {f['where']}." for f in had.get("floods") or []]
    if not parts:
        region = REGIONS.get(had.get("region") or "", ("",))[0]
        return f"No weather warnings{' for ' + region if region else ''}, and no flood warnings near here."
    return " ".join(p[0].upper() + p[1:] for p in parts)


def alerts(had: dict | None) -> list[tuple[str, int, str, str]]:
    """New warnings worth the DJ saying (conditions.alerts_now()): amber and red, flood warnings and severe ones.
    The level's in the key, so one that's raised (amber to red) is said again."""
    if not had:
        return []
    out = []
    for w in had.get("met") or []:
        if w["level"] in ("amber", "red"):
            words = _met_said(w, had.get("region"))
            out.append((f"met:{w['id']}:{w['level']}", MET_LEVELS.index(w["level"]), "The Met Office has issued " + words[0].lower() + words[1:],
                        f"{w['level'].capitalize()} warning: {w['what']}"))
    for f in had.get("floods") or []:
        if f["severity"] <= 2:
            out.append((f"flood:{f['id']}:{f['severity']}", 3 - f["severity"], f"The Environment Agency has issued a {f['kind']}: {f['where']}.",
                        f"{f['kind'].capitalize()}: {f['where']}"))
    return out


def tile(had: dict | None, alerted: set[str]) -> list[dict]:
    """The Warnings tile for "The world outside" (conditions.outside())."""
    if not had:
        return []
    ms, fs = had.get("met") or [], had.get("floods") or []
    top_met, top_flood = (ms[0] if ms else None), (fs[0] if fs else None)
    if top_flood and (top_flood["severity"] == 1 or not top_met or (top_flood["severity"] == 2 and top_met["level"] == "yellow")):
        value = {1: "SEVERE", 2: "FLOOD", 3: "ALERT"}[top_flood["severity"]]
        state, sub = ("poor" if top_flood["severity"] == 1 else "fair"), f"{top_flood['kind']} · {top_flood['where']}"
    elif top_met:
        value, state = top_met["level"].upper(), "poor" if top_met["level"] == "red" else "fair"
        sub = top_met["what"] + (f" · and {len(ms) + len(fs) - 1} more" if len(ms) + len(fs) > 1 else "")
    else:
        value, state = "none", "quiet"
        sub = f"none for {REGIONS[had['region']][0]}" if had.get("region") in REGIONS else "none near here"
    return [{"key": "warnings", "title": "Warnings", "value": value, "state": "alert" if "warnings" in alerted else state, "sub": sub,
             "says": says(had) + " (the Met Office and the Environment Agency)"}]
