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
stations are in, most first), how far, how that compares with a while ago
and with the same hour on the days before (usual(): the survey keeps a
week of each band, an hour apart), and a sentence that says so: "20 m is good: open to Europe and North
America, 16 stations, the farthest 5,516 km." What's heard depends on the
aerial and the receiver as much as the band: it's how the bands are *here*.

The weather teleprinters are a second yardstick (yardstick()): the German
weather service sends from near Hamburg, all day and night, on the same
few frequencies between 4.5 and 15 MHz with the same power. A ham band is
only as busy as the people on it; these are always there, so how strong
each is says how that part of the short waves is carrying from there to
here -- by day especially, when the lower ham bands are quiet for want of
people as much as of propagation. The survey measures each (its "db").

The sun's part comes from outside, when there's internet: NOAA's Space
Weather Prediction Center publishes the solar flux (how busy the sun is:
the higher, the higher the bands that open), the planetary K index (how
disturbed the earth's magnetic field is: 5 and over is a storm, which
closes paths over the poles and can shut the bands altogether) and its
scales for radio blackouts and storms. Sun fetches them, a few times a
day, and keeps what it last had. Two more come from outside the same way:

  * A forecast (Forecast): N0NBH's band conditions from hamqsl.com, the
    figures every ham site shows -- good, fair or poor for 80-40 m,
    30-20 m, 17-15 m and 12-10 m, by day and by night (the radio says the
    one it is now, by the sun at its locator) -- with the VHF openings it
    calculates: sporadic E on 6, 4 and 2 m in Europe, and aurora on VHF.
    It's a forecast, so it's said only when the radio hasn't heard the
    bands for itself.
  * AuroraWatch UK's alert (Aurora), from Lancaster University's
    magnetometers: green, yellow, amber or red, and what each means for
    seeing the aurora from the UK. Credited to them, as they ask, and
    asked for no more often than every quarter of an hour.

And with ALERTS on (off to begin with), the DJ says what's just happened
in the next gap, whatever the hour (alert_due()): AuroraWatch UK going to
amber or red, sporadic E opening on 6, 4 or 2 m, a strong radio blackout
or geomagnetic storm (R3 or G3 and over), a big earthquake (quakes.py).
Once each, when it starts.

Nothing here is said or shown unless it's asked for. It can be looked at
(the desktop's Bands window), and it can be said: with EVERY_H set (off to
begin with) the DJ reads a short version between songs, like the news --
say_due() is asked at each gap, and answers once in an hour that's one of
its hours, from a quarter past (the survey that goes by itself on the hour
has finished by then), outside the news's quiet hours, and only if
there's something recent to go by. spoken() is that short version: the
best band or two and what the sun is doing, in words a voice can say.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
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
USUAL_DAYS = 2               # this many earlier days must have been surveyed at about this hour to say what's usual
USUAL_NEAR_S = 5400.0        # ..."about this hour": within an hour and a half of it
USUAL_BY = 1.6               # this many times the usual (or that fraction of it) is better (or poorer) than usual
REGIONS = {"EU": "Europe", "NA": "North America", "SA": "South America", "AF": "Africa", "AS": "Asia", "OC": "Australia and the Pacific"}
K_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
FLUX_URL = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
SCALES_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
SUN_EVERY_S = 3 * 3600.0     # the K index is given every three hours
SUN_RETRY_S = 900.0          # ...and after a try that failed, not again for this long
SUN_OLD_S = 12 * 3600.0      # figures older than this aren't said as how the sun is
EVERY_H = 0                  # the DJ says how the bands are this many hours apart (0: not at all); main.py sets it from the settings
SAY_HOURS = (0, 1, 2, 3, 6)
SAY_FROM_MIN = 15            # ...from this far past the hour (clear of the news, and of the messages at 7 and 22 minutes past)
FORECAST_URL = "https://www.hamqsl.com/solarxml.php"
FORECAST_EVERY_S = 3600.0    # N0NBH's figures change every few hours: once an hour is plenty
FORECAST_OLD_S = 12 * 3600.0
AURORA_URL = "https://aurorawatch-api.lancs.ac.uk/0.2/status/current-status.xml"
AURORA_EVERY_S = 900.0       # AuroraWatch UK ask for no more than one request in three minutes
AURORA_OLD_S = 2 * 3600.0    # an alert this old isn't said as how it is now
AURORA_SITE = "https://aurorawatch.lancs.ac.uk/"
ALERTS = False               # the DJ says it when the aurora, an opening or a storm begins; main.py sets it from the settings
ALERT_AGAIN_S = 3 * 3600.0   # ...and the same one not again for this long, whatever it does in between


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


def usual(week: list, t: float) -> tuple[float, int] | None:
    """How many stations a band usually has at this hour: the mean, over the earlier days it was surveyed within
    USUAL_NEAR_S of this time of day, of what was heard then -> (that, how many days), or None with too few days."""
    days: dict[int, list[int]] = {}
    for at, n in week:
        ago = t - at
        d = round(ago / 86400)
        if d >= 1 and abs(ago - d * 86400) <= USUAL_NEAR_S:
            days.setdefault(d, []).append(n)
    if len(days) < USUAL_DAYS:
        return None
    return sum(sum(v) / len(v) for v in days.values()) / len(days), len(days)


def than_usual(n: float, norm: float) -> str:
    """"better", "poorer" or "usual": n against what's usual (small numbers either way are neither)."""
    if max(n, norm) < FAIR:
        return "usual"
    return "better" if n >= USUAL_BY * norm else "poorer" if n * USUAL_BY <= norm else "usual"


def band_says(band: str, n: int, km: int | None, to: dict[str, int] | None, before: int | None = None, hour: int | None = None,
              norm: tuple[float, int] | None = None) -> dict:
    """One band, from a count of stations in one slot -> {"band", "state", "n", "km"?, "to": [...], "says",
    "usual"? (stations at this hour on the days before), "days"? (how many of them), "than_usual"?}.
    hour: it's from the list of who's been heard, and this many were in the last hour (said so). norm: usual()."""
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
    extra = {}
    if norm is not None:
        extra = {"usual": round(norm[0], 1), "days": norm[1], "than_usual": than_usual(n, norm[0])}
        if extra["than_usual"] != "usual":
            says += (f" {'Better' if extra['than_usual'] == 'better' else 'Poorer'} than usual for this hour: "
                     f"about {round(norm[0])} on the last {norm[1]} days.")
    return {"band": band, "state": state, "n": n, **({"km": km} if km else {}), "to": where, "says": says, **extra}


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
        bands[band] = {**band_says(band, int(f["n"]), f.get("km"), f.get("to"), day[-1][1] if day else None,
                                   norm=usual(f.get("week", []), f["t"])), "t": f["t"], "by": "survey",
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
    known = [b for b in order if "usual" in b]         # all the bands together, against the same hour on the days before
    overall = than_usual(sum(b["n"] for b in known), sum(b["usual"] for b in known)) if len(known) >= 2 else None
    if overall in ("better", "poorer"):
        says += f" The bands are {'better' if overall == 'better' else 'poorer'} than usual for this hour."
    steady = yardstick(found, now)
    return {"bands": order, "says": says, "t": max((b["t"] for b in order), default=None), **({"than_usual": overall} if overall else {}),
            **({"weather": steady} if steady else {})}


STRONG_DB, FAIR_DB = 25.0, 12.0      # a weather teleprinter's tones this far over the noise: strong, fair (else weak)


def yardstick(found: dict, now: float) -> dict | None:
    """The weather teleprinters as the survey last found them -> {"at": [{"khz", "n", "db"?, "state": "strong" |
    "fair" | "weak" | "not heard", "t"}], "says"}, the lowest frequency first; None if none was listened to lately."""
    at = []
    for key, f in found.items():
        kind, _, khz = key.partition(":")
        if kind != "wx" or now - f.get("t", 0) > FRESH_S:
            continue
        try:
            khz = float(khz)
        except ValueError:
            continue
        db = f.get("db")
        state = "not heard" if not f.get("n") else "strong" if (db or 0) >= STRONG_DB else "fair" if (db or 0) >= FAIR_DB else "weak" if db is not None else "heard"
        at.append({"khz": khz, "n": int(f.get("n", 0)), **({"db": db} if db is not None else {}), "state": state, "t": f["t"]})
    if not at:
        return None
    at.sort(key=lambda a: a["khz"])
    mhz = lambda a: f"{a['khz'] / 1000:.1f}"
    groups = [(state, [mhz(a) for a in at if a["state"] == state]) for state in ("strong", "fair", "weak", "heard", "not heard")]
    parts = [f"{state} on {_and(where)} MHz" for state, where in groups if where]
    return {"at": at, "says": "The German weather teleprinters, a steady signal from Hamburg: " + ", ".join(parts) + "."}


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


class Kept:
    """Figures from a site, fetched now and then (in a thread of its own, when asked for and they're stale) and
    kept, in memory and in a file, for when the radio starts again or the internet goes. A kind of it gives
    EVERY_S (how stale), OLD_S (too old to say), _fetch() and _says()."""
    EVERY_S, RETRY_S, OLD_S = SUN_EVERY_S, SUN_RETRY_S, SUN_OLD_S
    NAME = "figures"

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time) -> None:
        self._get, self._clock = get, clock
        self._lock = threading.Lock()
        self._file: Path | None = None
        self._had: dict | None = None
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
        """Ask now. -> what was had, or None if it couldn't be reached."""
        try:
            had = {**self._fetch(), "t": self._clock()}
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, ET.ParseError) as e:
            log.info("conditions: %s weren't had (%s)", self.NAME, e)
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
        """What was last had, with "says", or None if there's nothing recent; a fetch is begun if it's stale."""
        now = self._clock()
        with self._lock:
            had = self._had
            stale = had is None or now - had["t"] > self.EVERY_S
            if stale and not self._busy and now - self._tried > self.RETRY_S:
                self._busy, self._tried = True, now
                threading.Thread(target=self._refresh, name=f"conditions-{type(self).__name__.lower()}", daemon=True).start()
        if had is None or now - had["t"] > self.OLD_S:
            return None
        return {**had, "says": self._says(had)}

    def _refresh(self) -> None:
        try:
            self.fetch()
        finally:
            with self._lock:
                self._busy = False

    def _fetch(self) -> dict:
        raise NotImplementedError

    def _says(self, had: dict) -> str:
        raise NotImplementedError


class Sun(Kept):
    """NOAA's figures for the sun and the earth's field: {"flux", "k", "a", "scales", "t"}."""
    NAME = "NOAA's figures"

    def _fetch(self) -> dict:
        ks = self._get(K_URL)
        rows = [row for row in ks if isinstance(row, dict) and row.get("Kp") is not None]
        if not rows:                                 # (as it used to give them: a list of lists under a line of headings)
            rows = [{"Kp": row[1], "a_running": row[2] if len(row) > 2 else None} for row in ks[1:] if isinstance(row, list) and len(row) > 1]
        k = rows[-1]
        flux = self._get(FLUX_URL)
        flux = flux[0] if isinstance(flux, list) else flux
        had = {"flux": float(flux.get("flux", flux.get("Flux"))), "k": float(k["Kp"]), "a": k.get("a_running")}
        try:
            had["scales"] = {key: {"Scale": (v or {}).get("Scale")} for key, v in (self._get(SCALES_URL).get("0") or {}).items() if key in ("R", "S", "G")}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return had

    def _says(self, had: dict) -> str:
        return sun_says(had.get("flux"), had.get("k"), had.get("scales"))


def _xml(url: str, timeout: float = 12.0) -> ET.Element:
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


# hamqsl.com's VHF phenomena, as it names them -> what they are, and the band
VHF = {("E-Skip", "europe_6m"): ("es", "6 m"), ("E-Skip", "europe_4m"): ("es", "4 m"), ("E-Skip", "europe"): ("es", "2 m"),
       ("vhf-aurora", "northern_hemi"): ("aurora", None)}
HF_GROUPS = ("80m-40m", "30m-20m", "17m-15m", "12m-10m")


def daytime(t: float, home: str | None = None) -> bool:
    """Is the sun up at the radio's locator at time t? (By the clock, 7 till 7, with no locator.)"""
    try:
        lat, lon = ft8.locate(home) if home else (None, None)
    except (ValueError, IndexError, TypeError, AttributeError):
        lat = lon = None
    if lat is None:
        return 7 <= time.localtime(t).tm_hour < 19
    n = (t - 946728000.0) / 86400                    # days from noon on 1 January 2000
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians((280.460 + 0.9856474 * n) % 360 + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    hour_angle = math.radians((18.697374558 + 24.06570982441908 * n) % 24 * 15 + lon) - ra
    la = math.radians(lat)
    return math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(hour_angle) > -0.0145   # (-0.83 degrees: the sun's edge on the horizon)


def _bands_named(groups: list[str]) -> str:
    """["30m-20m", "17m-15m"] -> "30 to 20 and 17 to 15 metres"."""
    return _and([g.replace("m-", " to ").rstrip("m") for g in groups]) + " metres"


def forecast_says(had: dict, day: bool) -> str:
    """N0NBH's forecast, for now (by day or by night), in a sentence."""
    when = "day" if day else "night"
    by = {}
    for g in HF_GROUPS:
        state = ((had.get("bands") or {}).get(g) or {}).get(when)
        if state:
            by.setdefault(state.lower(), []).append(g)
    parts = [f"{_bands_named(gs)} {state}" for state in ("good", "fair", "poor") if (gs := by.get(state))]
    says = (f"Forecast for {'today' if day else 'tonight'}: " + "; ".join(parts) + ".") if parts else ""
    opening = [band for (kind, band) in openings(had) if kind == "es"]
    if opening:
        says += f" Sporadic E: {_and(opening)} open in Europe."
    if any(kind == "aurora" for kind, _ in openings(had)):
        says += " Aurora on VHF" + (", down to mid latitudes." if "MID" in str((had.get("vhf") or {}).get("aurora", "")).upper() else ".")
    if had.get("noise"):
        says += f" Noise {had['noise']}."
    return says.strip()


def openings(had: dict | None) -> list[tuple[str, str | None]]:
    """The VHF openings in a forecast: [("es", "6 m"), ("aurora", None)], from the lowest band."""
    vhf = (had or {}).get("vhf") or {}
    return [(kind, band) for kind, band in sorted(VHF.values(), key=lambda kb: kb[1] or "") [::-1]
            if (v := vhf.get("aurora" if kind == "aurora" else band)) and "closed" not in str(v).lower()]


class Forecast(Kept):
    """N0NBH's band conditions (hamqsl.com): {"bands": {"80m-40m": {"day": "Fair", "night": "Good"}, ...},
    "vhf": {"6 m": "Band Closed", ..., "aurora": "Band Closed"}, "noise", "xray", "sunspots", "updated", "t"}."""
    EVERY_S, RETRY_S, OLD_S = FORECAST_EVERY_S, SUN_RETRY_S, FORECAST_OLD_S
    NAME = "N0NBH's forecast"

    def __init__(self, get: Callable = _xml, clock: Callable[[], float] = time.time) -> None:
        super().__init__(get, clock)

    def _fetch(self) -> dict:
        root = self._get(FORECAST_URL)
        data = root.find("solardata") if root.tag != "solardata" else root
        bands: dict[str, dict[str, str]] = {}
        for b in data.iter("band"):
            if b.get("name") in HF_GROUPS and b.get("time") in ("day", "night") and (b.text or "").strip():
                bands.setdefault(b.get("name"), {})[b.get("time")] = b.text.strip()
        if not bands:
            raise ValueError("no band conditions in it")
        vhf = {}
        for ph in data.iter("phenomenon"):
            kind = VHF.get((ph.get("name"), ph.get("location")))
            if kind and (ph.text or "").strip():
                vhf["aurora" if kind[0] == "aurora" else kind[1]] = ph.text.strip()
        text = lambda tag: (data.findtext(tag) or "").strip() or None
        sunspots = text("sunspots")
        return {"bands": bands, "vhf": vhf, "noise": text("signalnoise"), "xray": text("xray"),
                "sunspots": int(sunspots) if sunspots and sunspots.isdigit() else None, "updated": text("updated")}

    def _says(self, had: dict) -> str:
        return forecast_says(had, daytime(self._clock(), ft8.HOME))


AURORA_LEVELS = ("green", "yellow", "amber", "red")
AURORA_MEANS = {
    "green": "no significant activity",
    "yellow": "minor geomagnetic activity: the aurora might be seen by eye from Scotland, and by camera from northern England",
    "amber": "possible aurora: likely to be seen by eye from Scotland, northern England and Northern Ireland",
    "red": "aurora likely: it may be seen by eye from anywhere in the UK",
}


class Aurora(Kept):
    """AuroraWatch UK's alert: {"level": "green" | "yellow" | "amber" | "red", "site", "where"?, "credit"?, "t"}.
    Where the reading's from (the site's place and who to thank) is asked for only when the site changes."""
    EVERY_S, RETRY_S, OLD_S = AURORA_EVERY_S, AURORA_EVERY_S, AURORA_OLD_S
    NAME = "AuroraWatch UK's alert"

    def __init__(self, get: Callable = _xml, clock: Callable[[], float] = time.time) -> None:
        super().__init__(get, clock)

    def _fetch(self) -> dict:
        root = self._get(AURORA_URL)
        st = root.find("site_status")
        level = (st.get("status_id") or "").lower()
        if level not in AURORA_LEVELS:
            raise ValueError(f"no alert level in it ({level!r})")
        had = {"level": level, "site": st.get("site_id")}
        before = self._had or {}
        if before.get("site") == had["site"] and before.get("where"):
            had.update({k: before[k] for k in ("where", "credit") if k in before})
        elif st.get("site_url"):
            try:
                site = self._get(st.get("site_url").replace("http://", "https://", 1))
                place = site.find(f".//site[@id='{had['site']}']")
                where = place.findtext("location") if place is not None else None
                if where:
                    had["where"] = where.strip()
                    had["credit"] = " ".join(t.strip() for t in (place.findtext("attribution") or "", place.findtext("license") or "") if t.strip())
            except (OSError, ValueError, AttributeError, ET.ParseError):
                pass
        return had

    def _says(self, had: dict) -> str:
        return f"AuroraWatch UK: {had['level']}, {AURORA_MEANS[had['level']]}."


sun = Sun()
forecast = Forecast()
aurora = Aurora()


def freshest(job) -> tuple[str | None, dict]:
    """The receiver whose survey is the latest, and what it found: (its address, survey.Job.found() of it)."""
    best, when, found = None, 0.0, {}
    for base in job.bases():
        f = job.found(base, week=True)
        t = max((v.get("t", 0) for k, v in f.items() if k.startswith("ft8:")), default=0)
        if t > when:
            best, when, found = base, t, f
    return best, found


def _metres(band: str) -> str:
    return band.replace(" m", " metres")


def _forecast_spoken(fc: dict, day: bool) -> str | None:
    """N0NBH's forecast for now, as the DJ says it: the bands that are good (or fair, if none is)."""
    when = "day" if day else "night"
    by = {}
    for g in HF_GROUPS:
        state = ((fc.get("bands") or {}).get(g) or {}).get(when, "").lower()
        by.setdefault(state, []).append(g)
    for state in ("good", "fair"):
        if by.get(state):
            return f"The forecast {'today' if day else 'tonight'}: {_bands_named(by[state])} {state}."
    return "The forecast: the bands are poor." if by.get("poor") else None


def spoken(s: dict, sun_now: dict | None = None, fc: dict | None = None, aurora_now: dict | None = None, day: bool = True) -> str | None:
    """A summary() as the DJ says it: the two best bands, what's closed, and the sun in a few words; with nothing
    heard to go by, N0NBH's forecast instead (fc). Then any VHF opening, and the aurora if it's above green. None
    if there's nothing to say."""
    live = sorted((b for b in s["bands"] if b["n"]), key=lambda b: (-b["n"], -(b.get("km") or 0)))
    said = []
    if not s["bands"]:
        line = _forecast_spoken(fc, day) if fc else None
        if not line:
            return None
        said = ["The short wave bands.", line]
    elif not live:
        said = ["The short wave bands. Nothing heard on any band just now."]
    else:
        said = ["The short wave bands."]
        for b in live[:2]:
            line = f"{_metres(b['band'])} is {b['state']}"
            if b.get("km") and b["km"] < NEAR_KM:
                line += ", but only round about"
            elif b["to"]:
                line += f", open to {_and(b['to'])}"
            said.append(line + ".")
        rest = [_metres(b["band"]).replace(" metres", "") for b in live[2:]]
        if rest:
            said.append(f"Also open: {_and(rest)} metres.")
    if s.get("than_usual") in ("better", "poorer"):
        said.append(f"The bands are {s['than_usual']} than usual for this time of day.")
    if sun_now:
        flux, k = sun_now.get("flux"), sun_now.get("k")
        words = []
        if flux is not None:
            words.append("the sun is busy" if flux >= 150 else "the sun is middling" if flux >= 100 else "the sun is quiet")
        if k is not None:
            words.append("there's a magnetic storm" if k >= 5 else "the earth's field is unsettled" if k >= 4 else "the earth's field is quiet")
        if words:
            said.append((" and ".join(words) + ".").capitalize())
    es = [b.replace(" m", " metres") for kind, b in openings(fc) if kind == "es"]
    if es:
        said.append(f"Sporadic E is open on {_and(es)} across Europe.")
    if aurora_now and aurora_now.get("level") in ("yellow", "amber", "red"):
        said.append(f"AuroraWatch UK is at {aurora_now['level']}.")
    return " ".join(said)


_said: str | None = None             # the hour ("2026-10-12 14") it was last said in


def say_due(at, clock_ok: bool, quiet: bool, job, heard: list[dict]) -> str | None:
    """Is it time for the DJ to say how the bands are, in a gap at `at` (a datetime)? -> the words, once in an
    hour that's one of its hours; None otherwise, or if there's nothing recent to go by."""
    global _said
    if not EVERY_H or not clock_ok or quiet or at.hour % EVERY_H or at.minute < SAY_FROM_MIN:
        return None
    key = f"{at:%Y-%m-%d %H}"
    if _said == key:
        return None
    words = spoken(summary(freshest(job)[1], heard, at.timestamp()), sun.status(), forecast.status(), aurora.status(),
                   daytime(at.timestamp(), ft8.HOME))
    if words:
        _said = key
    return words


_alerted: dict[str, tuple[int, float]] = {}       # what's been alerted: {"aurora": (its level, when said), "es:6 m": ...}


def alerts_now(sun_now: dict | None, fc: dict | None, aurora_now: dict | None, quakes_now: dict | None = None) -> list[tuple[str, int, str, str]]:
    """What's worth an alert as things are: [(what, how high, the words, a few of them for the page)]."""
    from sleepradiopi.playback import quakes        # (it builds on Kept, here)
    out = quakes.alerts(quakes_now)
    if aurora_now and aurora_now.get("level") in ("amber", "red"):
        level = aurora_now["level"]
        out.append(("aurora", AURORA_LEVELS.index(level),
                    f"AuroraWatch UK has gone to {level}: {AURORA_MEANS[level]}.",
                    f"Aurora: {level} · {'possible' if level == 'amber' else 'likely'} tonight"))
    for kind, band in openings(fc):
        if kind == "es":
            out.append((f"es:{band}", 1, f"Sporadic E has opened on {band.replace(' m', ' metres')} across Europe.", f"Sporadic E open on {band}"))
    for letter, what, short in (("R", "a strong radio blackout on the sunlit side", "Radio blackout"), ("G", "a strong geomagnetic storm", "Geomagnetic storm")):
        level = str((((sun_now or {}).get("scales") or {}).get(letter) or {}).get("Scale") or "0")
        if level.isdigit() and int(level) >= 3:
            out.append((f"noaa:{letter}", int(level), f"NOAA reports {what}: {letter}{level}.", f"{short} {letter}{level}"))
    return out


def alert_due(at, clock_ok: bool, quiet: bool, now: float | None = None) -> str | None:
    """Anything just begun worth the DJ saying now (ALERTS on): an aurora alert at amber or red, a sporadic E
    opening, a radio blackout or storm at 3 or more -- each once when it starts or goes higher, and the same
    one not again for ALERT_AGAIN_S. What's ended is forgotten, so it's said again if it comes back after that."""
    if not ALERTS:
        return None
    now = time.time() if now is None else now
    from sleepradiopi.playback import quakes
    found = alerts_now(sun.status(), forecast.status(), aurora.status(), quakes.quakes.status())
    live = {what for what, *_ in found}
    for what in [w for w in _alerted if w not in live and now - _alerted[w][1] > ALERT_AGAIN_S]:
        del _alerted[what]
    if not clock_ok or quiet:
        return None
    said = []
    for what, level, words, _ in found:
        before = _alerted.get(what)
        if before is None or level > before[0]:
            _alerted[what] = (level, now)
            said.append(words)
    return " ".join(said) or None


# Which tile an alert belongs to (the desktop's "The world outside": outside())
ALERT_TILE = {"aurora": "aurora", "es": "vhf", "noaa": "sun", "quake": "quakes", "ukquake": "ukquakes"}


def outside(s: dict, sun_now: dict | None, fc: dict | None, aurora_now: dict | None, day: bool, quakes_now: dict | None = None) -> dict:
    """The desktop's "The world outside" folder: a tile for each thing known, and the alerts there are now ->
    {"tiles": [{"key", "title", "value" (a few letters, on the tile), "state" ("good" | "fair" | "poor" |
    "alert" | "quiet"), "sub", "says", "alert"}], "alerts": [{"key", "tile", "short", "says"}]}. A tile is
    left out when there's nothing to go by (no internet and nothing heard)."""
    alerts = [{"key": what, "tile": ALERT_TILE[what.partition(":")[0]], "short": short, "says": words}
              for what, _, words, short in alerts_now(sun_now, fc, aurora_now, quakes_now)]
    alerted = {a["tile"] for a in alerts}
    tiles = []
    live = sorted((b for b in s["bands"] if b["n"]), key=lambda b: (-b["n"], -(b.get("km") or 0)))
    if live:
        best = live[0]
        tiles.append({"key": "bands", "title": "Bands", "value": best["band"], "state": {"good": "good", "fair": "fair"}.get(best["state"], "poor"),
                      "sub": f"{best['state']} · heard here", "says": s["says"]})
    elif s["bands"]:
        tiles.append({"key": "bands", "title": "Bands", "value": "quiet", "state": "quiet", "sub": "nothing heard", "says": s["says"]})
    elif fc:
        when = "day" if day else "night"
        by = {}
        for g in HF_GROUPS:
            by.setdefault(((fc.get("bands") or {}).get(g) or {}).get(when, "").lower(), []).append(g)
        state = next((st for st in ("good", "fair", "poor") if by.get(st)), None)
        if state:
            tiles.append({"key": "bands", "title": "Bands", "value": state.upper(), "state": state,
                          "sub": f"{by[state][0].split('m-')[0]}–{by[state][-1].split('m-')[1].rstrip('m')} m {'today' if day else 'tonight'}",
                          "says": forecast_says(fc, day) + " (N0NBH's forecast: nothing heard here lately.)"})
    if sun_now:
        k, flux = sun_now.get("k"), sun_now.get("flux")
        tiles.append({"key": "sun", "title": "The sun", "value": f"K {round(k)}" if k is not None else f"{round(flux)}",
                      "state": "alert" if "sun" in alerted else "poor" if (k or 0) >= 5 else "fair" if (k or 0) >= 4 else "good",
                      "sub": " · ".join(x for x in (f"flux {round(flux)}" if flux is not None else "",
                                                    "storm" if (k or 0) >= 5 else "unsettled" if (k or 0) >= 4 else "quiet" if k is not None else "") if x),
                      "says": sun_now.get("says", "")})
    if aurora_now:
        level = aurora_now["level"]
        tiles.append({"key": "aurora", "title": "Aurora", "value": level.upper(),
                      "state": "alert" if "aurora" in alerted else {"green": "good", "yellow": "fair"}.get(level, "poor"),
                      "sub": "AuroraWatch UK", "says": aurora_now.get("says", "")})
    if fc:
        es = [band for kind, band in openings(fc) if kind == "es"]
        tiles.append({"key": "vhf", "title": "VHF openings", "value": f"{es[0]} Es" if es else "closed",
                      "state": "alert" if "vhf" in alerted else "quiet",
                      "sub": f"sporadic E · {_and(es)}" if es else "nothing open",
                      "says": f"Sporadic E open on {_and(es)} across Europe (N0NBH)." if es else "No sporadic E or VHF aurora forecast (N0NBH)."})
    from sleepradiopi.playback import quakes
    tiles += quakes.tiles(quakes_now, alerted)
    for t in tiles:
        t["alert"] = t["key"] in alerted
    return {"tiles": tiles, "alerts": alerts}
