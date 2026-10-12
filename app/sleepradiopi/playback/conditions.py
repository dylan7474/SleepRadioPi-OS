"""Band conditions: how the short-wave bands are, said plainly, from what the radio has itself heard.

Short wave isn't the same two hours running. A band "opens" to somewhere
when the ionosphere bends its signals back down there, and closes when it
stops: 20 m to North America in the afternoon, 40 m across Europe after
dark, 10 m to anywhere at all only when the sun is busy. Forecasts are
guesses; what's on the air is the fact, and the radio already gathers it:

  * The survey (playback/survey.py) goes round each band's FT8 frequency
    and counts the stations in one slot, with the farthest and where they
    all are -- by itself on the hour, if it's been asked to. FT8 is the
    best yardstick there is: it's busy on every band at every hour that
    the band is open at all.
  * The FT8 reader's list of who's been heard (audio/ft8.py), for the band
    being listened to: the last hour of it says the same thing.

summary() puts them together, a band at a time: how good ("good", "fair",
"poor", "nothing heard"), where it's open to (the parts of the world the
stations are in, most first), how far, how that compares with a while ago,
and a sentence that says so: "20 m is good: open to Europe and North
America, 16 stations, the farthest 5,516 km." What's heard depends on the
aerial and the receiver as much as the band: it's how the bands are *here*.

The sun's part comes from outside, when there's internet: NOAA's Space
Weather Prediction Center publishes the solar flux (how busy the sun is:
the higher, the higher the bands that open), the planetary K index (how
disturbed the earth's magnetic field is: 5 and over is a storm, which
closes paths over the poles and can shut the bands altogether) and its
scales for radio blackouts and storms. Sun fetches them, a few times a
day, and keeps what it last had.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

BANDS = ("160 m", "80 m", "60 m", "40 m", "30 m", "20 m", "17 m", "15 m", "12 m", "10 m", "6 m", "2 m")
FRESH_S = 2 * 3600.0         # a survey this old still says how a band is
HEARD_S = 3600.0             # ...and who's been heard in the last hour does
GOOD, FAIR = 15, 5           # stations in one FT8 slot: this many is a good band, this many a fair one
NEAR_KM = 1500               # everybody nearer than this: the band is open, but only round about
REGIONS = {"EU": "Europe", "NA": "North America", "SA": "South America", "AF": "Africa", "AS": "Asia", "OC": "Australia and the Pacific"}
K_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
FLUX_URL = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
SCALES_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
SUN_EVERY_S = 3 * 3600.0     # the K index is given every three hours
SUN_RETRY_S = 900.0          # ...and after a try that failed, not again for this long
SUN_OLD_S = 12 * 3600.0      # figures older than this aren't said as how the sun is


def region(grid: str) -> str | None:
    """The part of the world a locator is in: "EU", "NA", "SA", "AF", "AS" or "OC"; None for the oceans between."""
    try:
        lat, lon = ft8.locate(grid)
    except (ValueError, IndexError, TypeError, AttributeError):
        return None
    if lat >= 35 and -25 <= lon < 45:
        return "EU"
    if lat >= 12 and -170 <= lon < -50:
        return "NA"
    if lat < 12 and -92 <= lon < -30:
        return "SA"
    if -36 <= lat < 37 and -20 <= lon < 52:
        return "AF"
    if lat >= -10 and 45 <= lon <= 180:
        return "AS"
    if lat < -10 and (lon >= 110 or lon < -130):
        return "OC"
    return None


def regions(grids) -> dict[str, int]:
    """How many of these locators are in each part of the world."""
    out: dict[str, int] = {}
    for g in grids:
        r = region(g)
        if r:
            out[r] = out.get(r, 0) + 1
    return out


def _to(counts: dict[str, int]) -> list[str]:
    """The parts of the world worth naming, most stations first (one station in ten, at least, or two)."""
    total = sum(counts.values())
    return [REGIONS[r] for r, n in sorted(counts.items(), key=lambda kv: -kv[1]) if r in REGIONS and (n >= 2 or n * 10 >= total)]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def band_says(band: str, n: int, km: int | None, to: dict[str, int] | None, before: int | None = None, hour: int | None = None) -> dict:
    """One band, from a count of stations in one slot -> {"band", "state", "n", "km"?, "to": [...], "says"}.
    hour: it's from the list of who's been heard, and this many were in the last hour (said so)."""
    state = "good" if n >= GOOD else "fair" if n >= FAIR else "poor" if n else "nothing heard"
    where = _to(to or {})
    if not n:
        says = f"{band}: nothing heard."
    else:
        says = f"{band} is {state}"
        if km and km < NEAR_KM:
            says += f": open, but only round about ({n} station{'' if n == 1 else 's'}, none past {km:,} km)"
        else:
            count = f"{hour} station{'' if hour == 1 else 's'} in the last hour" if hour is not None else f"{n} station{'' if n == 1 else 's'}"
            says += (f": open to {_and(where)}" if where else "") + f", {count}" + (f", the farthest {km:,} km" if km else "")
        says += "."
    if before is not None and min(n, before) >= 1 and max(n, before) >= 5 and (n >= 2 * before or before >= 2 * n):
        says += " Busier than it was." if n > before else " Quieter than it was."
    elif before is not None and bool(n) != bool(before) and max(n, before) >= 3:
        says += " It has opened." if n else " It has closed."
    return {"band": band, "state": state, "n": n, **({"km": km} if km else {}), "to": where, "says": says}


def summary(found: dict, heard: list[dict], now: float) -> dict:
    """How the bands are -> {"bands": [{"band", "state", "n", "km"?, "to", "says", "t" (when that was), "by":
    "survey" | "heard", "day"?: [[t, n]]}], "says": the best of it in a sentence, "t": the latest of them}.
    found: a receiver's survey (survey.Job.found()); heard: who the FT8 reader has heard (its heard()["heard"])."""
    bands = {}
    for key, f in found.items():
        mode, _, band = key.partition(":")
        if mode != "ft8" or band not in BANDS or now - f.get("t", 0) > FRESH_S:
            continue
        day = [d for d in f.get("day", []) if d[0] < f["t"] - 60]
        bands[band] = {**band_says(band, int(f["n"]), f.get("km"), f.get("to"), day[-1][1] if day else None), "t": f["t"], "by": "survey",
                       **({"day": f["day"]} if len(f.get("day", [])) > 1 else {})}
    by_band: dict[str, list[dict]] = {}
    for h in heard:                                  # (the reader hears a band for as long as it's listened to: more stations than one slot's)
        if h.get("band") in BANDS and now - h.get("last", 0) <= HEARD_S:
            by_band.setdefault(h["band"], []).append(h)
    for band, who in by_band.items():
        t = max(h["last"] for h in who)
        if band in bands and bands[band]["t"] >= t - 600:
            continue
        slot = max(1, round(len(who) / 3))           # (roughly what one slot of them would be: an hour's list is several slots' worth)
        far = max((h.get("km") or 0 for h in who), default=0)
        bands[band] = {**band_says(band, slot, far or None, regions(h["grid"] for h in who if h.get("grid")), hour=len(who)), "t": t, "by": "heard", "heard": len(who)}
    order = [bands[b] for b in BANDS if b in bands]
    open_ = sorted((b for b in order if b["n"]), key=lambda b: (-b["n"], -(b.get("km") or 0)))
    if not order:
        says = "Nothing to go by yet: a survey of FT8 on every band says how the bands are."
    elif not open_:
        says = "Nothing heard on any band surveyed."
    else:
        best, far = open_[0], max(open_, key=lambda b: b.get("km") or 0)
        says = f"{best['band']} is the busiest" + (f", open to {_and(best['to'])}" if best["to"] else "") + "."
        if far is not best and (far.get("km") or 0) > (best.get("km") or 0):
            says += f" The longest reach is on {far['band']}: {far['km']:,} km."
        closed = [b["band"] for b in order if not b["n"]]
        if closed:
            says += f" Nothing on {_and(closed)}."
    return {"bands": order, "says": says, "t": max((b["t"] for b in order), default=None)}


def sun_says(flux: float | None, k: float | None, scales: dict | None = None) -> str:
    """The sun's figures, said: what they mean for the bands."""
    parts = []
    if flux is not None:
        parts.append(f"Solar flux {round(flux)}: " + ("the sun is busy, and the high bands (15, 12 and 10 m) should open by day" if flux >= 150
                     else "middling: 20 and 17 m by day, 15 m some days" if flux >= 100 else "the sun is quiet: 20 m and below"))
    if k is not None:
        parts.append(f"K index {k:g}: " + ("a magnetic storm, the bands are disturbed, paths over the poles most of all" if k >= 5
                     else "unsettled, the higher latitudes are disturbed" if k >= 4 else "the earth's field is quiet"))
    for letter, what in (("R", "a radio blackout on the sunlit side"), ("G", "a geomagnetic storm")):
        level = str(((scales or {}).get(letter) or {}).get("Scale") or "0")
        if level not in ("0", "None"):
            parts.append(f"NOAA has {what} at {letter}{level}")
    return ". ".join(parts) + ("." if parts else "")


def _get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Sun:
    """NOAA's figures for the sun and the earth's field, fetched now and then (in a thread of its own, when
    asked and they're stale) and kept."""

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time) -> None:
        self._get, self._clock = get, clock
        self._lock = threading.Lock()
        self._file: Path | None = None
        self._had: dict | None = None                # {"flux", "k", "a", "scales", "t"}
        self._tried = 0.0
        self._busy = False

    def keep_in(self, file: Path | None) -> None:
        self._file = file
        if file is None:
            return
        try:
            had = json.loads(file.read_text())
            if isinstance(had, dict) and isinstance(had.get("t"), (int, float)):
                self._had = had
        except (OSError, ValueError):
            pass

    def fetch(self) -> dict | None:
        """Ask NOAA now. -> what it said, or None if it couldn't be reached."""
        try:
            ks = self._get(K_URL)
            rows = [row for row in ks if isinstance(row, dict) and row.get("Kp") is not None]
            if not rows:                             # (as it used to give them: a list of lists under a line of headings)
                rows = [{"Kp": row[1], "a_running": row[2] if len(row) > 2 else None} for row in ks[1:] if isinstance(row, list) and len(row) > 1]
            k = rows[-1]
            flux = self._get(FLUX_URL)
            flux = flux[0] if isinstance(flux, list) else flux
            had = {"flux": float(flux.get("flux", flux.get("Flux"))), "k": float(k["Kp"]), "a": k.get("a_running"), "t": self._clock()}
            try:
                had["scales"] = {key: {"Scale": (v or {}).get("Scale")} for key, v in (self._get(SCALES_URL).get("0") or {}).items() if key in ("R", "S", "G")}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
            log.info("conditions: NOAA's figures weren't had (%s)", e)
            return None
        with self._lock:
            self._had = had
        if self._file is not None:
            try:
                write_atomic(self._file, json.dumps(had))
            except OSError:
                pass
        return had

    def status(self) -> dict | None:
        """{"flux", "k", "a", "scales", "t", "says"}, or None if there's nothing recent; a fetch is begun if they're stale."""
        now = self._clock()
        with self._lock:
            had = self._had
            stale = had is None or now - had["t"] > SUN_EVERY_S
            if stale and not self._busy and now - self._tried > SUN_RETRY_S:
                self._busy, self._tried = True, now
                threading.Thread(target=self._refresh, name="conditions-sun", daemon=True).start()
        if had is None or now - had["t"] > SUN_OLD_S:
            return None
        return {**had, "says": sun_says(had.get("flux"), had.get("k"), had.get("scales"))}

    def _refresh(self) -> None:
        try:
            self.fetch()
        finally:
            with self._lock:
                self._busy = False


sun = Sun()
