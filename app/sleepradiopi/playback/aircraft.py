"""Aircraft overhead, from a dump1090 / readsb receiver on the local network.

Such a receiver serves its picture as JSON: <address>/data/aircraft.json (every aircraft heard: callsign,
altitude, speed, track, position...) and <address>/data/receiver.json (where its aerial is). The owner types the
receiver's address into the Aircraft window (the setting aircraft_url: "192.168.1.5:8080" or a full
"http://host:port"; empty = off, and the address is never in the source). Only that address is asked.

It's a LAN thing, so it comes and goes: Aircraft (a conditions.Kept) asks every EVERY_S, keeps nothing on the
card, and the tile and window exist only while the receiver answers (a radio taken away from home, or the
receiver switched off, simply has no Aircraft tile after OLD_S, and no "something's wrong" light for it).
Squawk 7500 / 7600 / 7700 (hijack, radio failure, general emergency) is an alert, said by the DJ with Alerts on.
"""
from __future__ import annotations

import logging
import math
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept
from sleepradiopi.playback.lightning import compass

log = logging.getLogger(__name__)

MAP_FILE = Path(__file__).resolve().parent.parent / "data" / "map.json"      # the scope's built-in map (tools/build_mapdata.py): coast, towns, airfields
ADDRESS = ""                 # the receiver's address; main.py sets it from the settings (and the web side when it's changed)
EVERY_S = 20.0               # (Outside looks every 30 s, so about that)
OLD_S = 120.0                # a picture this old is no picture: the tile goes
NEAREST = 30                 # the nearest this many with a position are kept for the window (the scope plots them all)
LIST_MOST = 40               # ...and this many of the rest (callsigns only)
LOW_FT = 3000                # "low": below this
LOW_ALERT_FT = 2000          # "low and close": under this height...
LOW_ALERT_NM = 3.0           # ...and within this far of the aerial (in the air: not one on the ground)
DEAF_S = 3600.0              # a receiver that hears no aircraft at all for this long is deaf (aerial, dongle...)
WATCH = ""                   # what to watch for ("RRR, ASCOT, #43C0A1, mil"); main.py sets it from the settings (aircraft_watch)
WATCH_MOST = 10
MIL = ((0x43C000, 0x43CFFF), (0xAE0000, 0xAFFFFF))      # UK and US military addresses
SPEAK_EXTRAS = False         # low-and-close and watched aircraft are for the eye only (the tile, the folder's badge) until this is True
OFF: set[str] = set()        # kinds of alert switched off (KINDS); main.py sets it from the settings (aircraft_off)
QUIET: Callable[[], bool] = lambda: False       # whether it's the quiet hours now; main.py sets it
KINDS = {                    # the eye-only alerts: {kind: (name, what it is)}; the emergency squawk is always on
    "low": ("Low and close", f"Under {LOW_ALERT_FT:,} ft within {LOW_ALERT_NM:g} nm"),
    "watch": ("Watch list", "An aircraft matching Watch for"),
    "inbound": ("Heading for you", "On course to pass within 2 nm in the next 8 minutes"),
    "loiter": ("Circling", "Within 5 nm of one spot for 15 minutes: helicopters, survey flights, orbits"),
    "descent": ("Emergency descent", "Falling faster than 4,000 ft/min from over 10,000 ft"),
    "long": ("Long range", "Heard beyond 250 nm: a lift in conditions, or a good aerial day"),
    "night": ("Night flights", "Under 10,000 ft within 5 nm in the quiet hours"),
    "heli": ("Helicopters", "A helicopter within 10 nm (those that send their category)"),
    "heavy": ("Heavy jets low", "A heavy jet under 10,000 ft within 10 nm (those that send their category)"),
    "busy": ("Busy sky", "Twice as many aircraft as usual (once it has watched for three hours)"),
}
INBOUND_MIN, INBOUND_NM, INBOUND_FAR_NM = 8.0, 2.0, 80.0
LOITER_S, LOITER_NM, LOITER_MOST, LOITER_SAMPLES = 900.0, 5.0, 3, 8
TRACK_S, GAP_S = 1500.0, 300.0                   # a track is kept this long; a gap this long ends it (a new aircraft, or out of range)
DESCENT_RATE, DESCENT_AGAIN, DESCENT_FT, DESCENT_SHOWN_S = -4000, -3000, 10000, 600.0
LONG_NM = 250.0
NIGHT_FT, NIGHT_NM, NEAR_MOST = 10000, 5.0, 5
HELI_NM, HEAVY_NM, HEAVY_FT = 10.0, 10.0, 10000
BUSY_X, BUSY_MIN, BUCKET_S, HIST_S, BUSY_NEED = 2.0, 25, 1800.0, 86400.0, 6      # (buckets of half an hour, kept a day; 6 of them before "usual" means anything)
DRIFT_RATIO, DRIFT_MIN_NM, DRIFT_NEED, DRIFT_SAMPLES = 0.35, 60.0, 24, 20        # (the last hour's best range under 35% of the day's usual best, from 12 h of watching)
EMERGENCY = {"7500": "hijack", "7600": "radio failure", "7700": "a general emergency"}
M_TO_NM = 1 / 1852
SHORT = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def base(address: str) -> str | None:
    """What was typed -> "http://host:port" (no path), or None if it won't do. "192.168.1.5:8080" and
    "http://host:8080/anything" both work; only http and https."""
    a = (address or "").strip()
    if not a or len(a) > 200 or any(c.isspace() for c in a):
        return None
    if "://" not in a:
        a = "http://" + a
    u = urllib.parse.urlparse(a)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    try:
        u.port
    except ValueError:
        return None
    return f"{u.scheme}://{u.netloc}"


def parse_watch(text: str) -> list[tuple]:
    """What to watch for -> matchers: "RRR" a callsign starting so, "#43C0A1" one address, "#43C000-43CFFF" a range of
    addresses, "mil" the UK and US military ranges; commas or spaces between. A bad one: ValueError(it)."""
    out: list[tuple] = []
    for tok in re.split(r"[,\s]+", (text or "").strip().upper()):
        if not tok:
            continue
        if tok == "MIL":
            out += [("hex", lo, hi) for lo, hi in MIL]
        elif tok.startswith("#"):
            m = re.fullmatch(r"#([0-9A-F]{6})(?:-([0-9A-F]{6}))?", tok)
            if not m:
                raise ValueError(tok)
            lo = int(m.group(1), 16)
            hi = int(m.group(2), 16) if m.group(2) else lo
            if hi < lo:
                raise ValueError(tok)
            out.append(("hex", lo, hi))
        elif re.fullmatch(r"[A-Z0-9]{2,8}", tok):
            out.append(("call", tok))
        else:
            raise ValueError(tok)
    return out


def watched(p: dict, matchers: list[tuple]) -> bool:
    for m in matchers:
        if m[0] == "call":
            if (p.get("call") or "").upper().startswith(m[1]):
                return True
        else:
            try:
                if m[1] <= int(p.get("hex") or "", 16) <= m[2]:
                    return True
            except ValueError:
                pass
    return False


def _get(url: str, timeout: float = 6.0):
    import json
    req = urllib.request.Request(url, headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def distance(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """-> (nautical miles, bearing in degrees) from the first place to the second."""
    p1, p2, dl = math.radians(lat0), math.radians(lat), math.radians(lon - lon0)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371 * 2 * math.asin(math.sqrt(a))
    deg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    return km * 1000 * M_TO_NM, deg


def plane(a: dict, at: tuple[float, float] | None) -> dict | None:
    """One entry of aircraft.json -> {"hex", "call"?, "ft"?, "kt"?, "track"?, "climb"? ("up" | "down"), "squawk"?, "nm"?, "deg"?}
    or None (it has nothing but its address)."""
    out: dict = {"hex": str(a.get("hex") or "")[:8]}
    call = str(a.get("flight") or "").strip()
    if call:
        out["call"] = call[:10]
    alt = a.get("alt_baro")
    if isinstance(alt, (int, float)) and not isinstance(alt, bool):
        out["ft"] = round(alt)
    elif alt == "ground":
        out["ground"] = True
    if isinstance(a.get("gs"), (int, float)):
        out["kt"] = round(a["gs"])
    if isinstance(a.get("track"), (int, float)):
        out["track"] = round(a["track"]) % 360
    rate = a.get("baro_rate", a.get("geom_rate"))
    if isinstance(rate, (int, float)) and abs(rate) >= 300:
        out["climb"] = "up" if rate > 0 else "down"
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        out["rate"] = round(rate)
    if isinstance(a.get("category"), str) and len(a["category"]) == 2:
        out["cat"] = a["category"].upper()
    sq = str(a.get("squawk") or "")
    if sq in EMERGENCY:
        out["squawk"] = sq
    if at and isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float)):
        nm, deg = distance(at[0], at[1], a["lat"], a["lon"])
        out["nm"], out["deg"] = round(nm, 1), round(deg)
    return out if len(out) > 1 else None


def cpa(p: dict) -> tuple[float, float] | None:
    """Heading for the aerial: (minutes until it's nearest, how near in nm) if it will pass within INBOUND_NM of it in the next
    INBOUND_MIN minutes, else None. From where it is now (nm, deg), its track and its speed."""
    if p.get("ground") or "nm" not in p or "track" not in p or p.get("kt", 0) < 60 or p["nm"] > INBOUND_FAR_NM:
        return None
    e, n = p["nm"] * math.sin(math.radians(p["deg"])), p["nm"] * math.cos(math.radians(p["deg"]))
    v = p["kt"] / 60.0
    ve, vn = v * math.sin(math.radians(p["track"])), v * math.cos(math.radians(p["track"]))
    t = -(e * ve + n * vn) / (ve * ve + vn * vn)
    if not 0.5 <= t <= INBOUND_MIN:
        return None
    miss = math.hypot(e + ve * t, n + vn * t)
    return (round(t, 1), round(miss, 1)) if miss <= INBOUND_NM else None


def circling(track: list, now: float) -> tuple[float, float] | None:
    """A track [(t, lat, lon, ft)] that has stayed within LOITER_NM of its own middle for LOITER_S: (minutes, radius in nm)."""
    if len(track) < LOITER_SAMPLES or now - track[-1][0] > 120 or track[-1][0] - track[0][0] < LOITER_S:
        return None
    lat0, lon0 = sum(t[1] for t in track) / len(track), sum(t[2] for t in track) / len(track)
    far = max(distance(lat0, lon0, t[1], t[2])[0] for t in track)
    return (round((track[-1][0] - track[0][0]) / 60), round(far, 1)) if far <= LOITER_NM else None


class Aircraft(Kept):
    """{"n": all heard, "placed": with a position, "planes": the nearest NEAREST (nearest first) then the rest by
    callsign, "emergency": [those squawking 7500/7600/7700], "low": [low and close], "watch": [matching WATCH], "far_nm": the farthest, "aerial": whether distances
    are known, "from": "receiver" | "locator" | None (where the aerial's place came from), "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the aircraft receiver"
    LAN = True               # (a local-network thing: not asked for in the "something's wrong" light when it's not there)

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time, address: Callable[[], str] = lambda: ADDRESS,
                 watch: Callable[[], str] = lambda: WATCH) -> None:
        super().__init__(get, clock)
        self._address, self._watch = address, watch
        self._empty_since: float | None = None       # since when it has heard no aircraft at all (while it answered)
        self._mem = threading.Lock()                 # (what's below is for the fetching thread and the health thread)
        self._tracks: dict[str, list] = {}           # each aircraft's recent positions: {hex: [(t, lat, lon, ft)]}; memory only, never the card
        self._rates: dict[str, int] = {}             # the last climb rate of each, for "falling fast, twice running"
        self._descents: dict[str, tuple[float, dict]] = {}
        self._hist: dict[int, list] = {}             # {half hour: [farthest nm, sum of counts, samples]}, a day of it, for "usual"
        self._where: tuple[str, tuple[float, float] | None, str | None] | None = None      # (the base it was for, the aerial's place, where that came from)

    def _aerial(self, url: str) -> tuple[tuple[float, float] | None, str | None]:
        """Where the aerial is, and where that came from ("receiver": its receiver.json, asked once for an address and
        again if it wasn't had; "locator": without it, the middle of the radio's own locator square, which is only as
        close as the locator is). -> (place or None, "receiver" | "locator" | None)."""
        if self._where and self._where[0] == url and self._where[1]:
            return self._where[1], self._where[2]
        at, src = None, None
        try:
            r = self._get(url + "/data/receiver.json")
            if isinstance(r.get("lat"), (int, float)) and isinstance(r.get("lon"), (int, float)) and (r["lat"] or r["lon"]):
                at, src = (float(r["lat"]), float(r["lon"])), "receiver"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if at is None and ft8.HOME:
            try:
                at, src = tuple(ft8.locate(ft8.HOME)), "locator"
            except (ValueError, IndexError, TypeError, AttributeError):
                at = None
        self._where = (url, at, src)
        return at, src

    def _fetch(self) -> dict:
        url = base(self._address())
        if not url:
            raise ValueError("no receiver address")
        data = self._get(url + "/data/aircraft.json")
        raw = data["aircraft"]
        if not isinstance(raw, list):
            raise ValueError("no aircraft list")
        at, src = self._aerial(url)
        planes = [p for p in (plane(a, at) for a in raw if isinstance(a, dict)) if p]
        placed = sorted((p for p in planes if "nm" in p), key=lambda p: p["nm"])
        rest = sorted((p for p in planes if "nm" not in p and p.get("call")), key=lambda p: p["call"])
        emergency = [p for p in planes if p.get("squawk")]
        low = [p for p in placed if p.get("ft") is not None and p["ft"] < LOW_ALERT_FT and p["nm"] <= LOW_ALERT_NM and not p.get("ground")]
        try:
            matchers = parse_watch(self._watch())
        except ValueError:
            matchers = []
        watch = [p for p in sorted(planes, key=lambda p: p.get("nm", 1e9)) if watched(p, matchers)][:WATCH_MOST]
        now = self._clock()
        self._empty_since = (self._empty_since if self._empty_since is not None else now) if not raw else None
        far_nm = placed[-1]["nm"] if placed else None
        extra = self._watched_over(now, [(a, plane(a, at)) for a in raw if isinstance(a, dict)], placed, far_nm, len(raw))
        return {"n": len(raw), "placed": len(placed), "planes": placed[:NEAREST] + rest[:LIST_MOST], "emergency": emergency,
                "low": low, "watch": watch, "far_nm": far_nm, "aerial": bool(at), "from": src, "at": [round(at[0], 3), round(at[1], 3)] if at else None, **extra}

    def _watched_over(self, now: float, pairs: list, placed: list[dict], far_nm: float | None, n: int) -> dict:
        """What the picture says over time, kept in memory: tracks (circling), climb rates (falling fast), the day's counts and ranges
        (busy, and the range drifting), and the rest that is a look at one picture."""
        by_hex = {p["hex"]: p for p in placed}
        raw_of = {p["hex"]: a for a, p in pairs if p}
        inbound = []
        for p in placed:
            c = cpa(p)
            if c:
                inbound.append({**p, "eta": c[0], "miss": c[1]})
        inbound.sort(key=lambda p: p["eta"])
        with self._mem:
            for a, p in pairs:
                if not p:
                    continue
                h = p["hex"]
                if isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float)) and not p.get("ground"):
                    tr = self._tracks.setdefault(h, [])
                    if tr and now - tr[-1][0] > GAP_S:
                        tr.clear()
                    tr.append((now, a["lat"], a["lon"], p.get("ft")))
            for h in [h for h, tr in self._tracks.items() if not tr or now - tr[-1][0] > GAP_S]:
                del self._tracks[h]
            for tr in self._tracks.values():
                while tr and now - tr[0][0] > TRACK_S:
                    tr.pop(0)
            loiter = []
            for h, tr in self._tracks.items():
                c = circling(tr, now)
                if c and h in by_hex:
                    loiter.append({**by_hex[h], "mins": c[0], "radius": c[1]})
            loiter.sort(key=lambda p: -p["mins"])
            for a, p in pairs:                                                           # falling fast twice running, from high up
                if p and p.get("rate") is not None:
                    prev = self._rates.get(p["hex"])
                    if p["rate"] <= DESCENT_RATE and prev is not None and prev <= DESCENT_AGAIN and (p.get("ft") or 0) >= DESCENT_FT - 2000 and not p.get("ground"):
                        self._descents[p["hex"]] = (now, p)
                    self._rates[p["hex"]] = p["rate"]
            for h in [h for h in self._rates if h not in raw_of]:
                del self._rates[h]
            for h in [h for h, (t, _) in self._descents.items() if now - t > DESCENT_SHOWN_S]:
                del self._descents[h]
            descent = [p for _, p in self._descents.values()]
            b = int(now // BUCKET_S)
            rec = self._hist.setdefault(b, [0.0, 0, 0])
            rec[0], rec[1], rec[2] = max(rec[0], far_nm or 0.0), rec[1] + n, rec[2] + 1
            for old in [k for k in self._hist if now - k * BUCKET_S > HIST_S]:
                del self._hist[old]
            usual = [r[1] / r[2] for k, r in self._hist.items() if k < b and r[2]]
            busy = None
            if len(usual) >= BUSY_NEED:
                med = statistics.median(usual)
                if med > 0 and n >= BUSY_MIN and n >= BUSY_X * med:
                    busy = {"n": n, "usual": round(med)}
        trusted = lambda p: not any("lat" in (raw_of[p["hex"]].get(k) or []) for k in ("mlat", "tisb")) if p["hex"] in raw_of else True
        grounded = lambda p: p.get("ground")
        return {"inbound": inbound[:NEAREST // 6], "loiter": loiter[:LOITER_MOST], "descent": descent[:3], "busy": busy,
                "long": sorted((p for p in placed if p["nm"] >= LONG_NM and trusted(p)), key=lambda p: -p["nm"])[:3],
                "near": [p for p in placed if p.get("ft") is not None and p["ft"] < NIGHT_FT and p["nm"] <= NIGHT_NM and not grounded(p)][:NEAR_MOST],
                "heli": [p for p in placed if p.get("cat") == "A7" and p["nm"] <= HELI_NM and not grounded(p)][:NEAR_MOST],
                "heavy": [p for p in placed if p.get("cat") == "A5" and p["nm"] <= HEAVY_NM and p.get("ft") is not None and p["ft"] < HEAVY_FT and not grounded(p)][:NEAR_MOST]}

    def fetch(self) -> dict | None:
        had = super().fetch()
        if had is None:
            self._empty_since = None                 # (it didn't answer: nothing is known about what it hears)
        return had

    def trouble(self) -> list[dict]:
        """Things for the "something's wrong" light: a receiver that answers but has heard no aircraft at all for DEAF_S; and one
        whose best range over the last hour is far below its usual (a loose aerial lead, a failing dongle)."""
        out = []
        now = self._clock()
        since = self._empty_since
        if since is not None and self.wanted() and now - since >= DEAF_S:
            out.append({"key": "aircraft-deaf", "level": "warn", "title": "Aircraft receiver hears nothing",
                        "text": f"The aircraft receiver answers but has heard no aircraft for {int((now - since) // 3600)} h or more: check its aerial and dongle."})
        with self._mem:
            b = int(now // BUCKET_S)
            older = [r[0] for k, r in self._hist.items() if k < b - 1 and r[2] and r[0] > 0]
            recent = [self._hist[k] for k in (b, b - 1) if k in self._hist]
        if self.wanted() and len(older) >= DRIFT_NEED and sum(r[2] for r in recent) >= DRIFT_SAMPLES and self._empty_since is None:
            usual, best = statistics.median(older), max(r[0] for r in recent)
            if usual >= DRIFT_MIN_NM and best < DRIFT_RATIO * usual:
                out.append({"key": "aircraft-range", "level": "warn", "title": "Aircraft range has dropped",
                            "text": f"The aircraft receiver's best range in the last hour was {best:.0f} nm; it is usually about {usual:.0f} nm. A loose or damaged aerial lead is the likely cause (it can also be a quiet night)."})
        return out

    def wanted(self) -> bool:
        return base(self._address()) is not None

    def refresh(self) -> None:
        if self.wanted():                            # (no address: nothing to ask)
            super().refresh()

    def status(self, refresh: bool = True, old_ok: bool = False) -> dict | None:
        if not self.wanted():
            return None
        return super().status(refresh, False)        # (an old picture of the sky is no use: never "old", it just goes)

    def forget(self) -> None:
        """What was had, gone (a new address, or none)."""
        with self._lock:
            self._had, self._tried, self._where, self._empty_since = None, 0.0, None, None
        with self._mem:
            self._tracks.clear(), self._rates.clear(), self._descents.clear()
            self._hist.clear()

    def _says(self, had: dict) -> str:
        return says(had)


aircraft = Aircraft()


def label(p: dict) -> str:
    return p.get("call") or p.get("hex") or "an aircraft"


def where(p: dict) -> str:
    """"12 nm NE, 3,400 ft, heading 180"."""
    bits = []
    if "nm" in p:
        bits.append(f"{p['nm']:g} nm {compass(p['deg'])}")
    if p.get("ground"):
        bits.append("on the ground")
    elif "ft" in p:
        bits.append(f"{p['ft']:,} ft")
    if "track" in p:
        bits.append(f"heading {p['track']}")
    return ", ".join(bits)


def says(had: dict) -> str:
    placed = [p for p in had.get("planes") or [] if "nm" in p]
    if not had.get("n"):
        return "No aircraft heard just now."
    s = f"{had['n']} aircraft heard" + (f", {had['placed']} with a position" if had.get("placed") != had["n"] else "")
    if placed:
        s += f". Nearest: {label(placed[0])}, {where(placed[0])}"
    return s + "."


def tile(had: dict | None, alerted: set[str]) -> list[dict]:
    """The Aircraft tile for "The world outside" (conditions.outside()): there only while the receiver answers."""
    if not had:
        return []
    placed = [p for p in had.get("planes") or [] if "nm" in p]
    near = placed[0] if placed else None
    low = [p for p in placed if p.get("ft") is not None and p["ft"] < LOW_FT and not p.get("ground")]
    now = alerts(had)
    return [{"key": "aircraft", "title": "Aircraft", "value": str(had["n"]),
             "state": "alert" if "aircraft" in alerted else "good" if had["n"] else "quiet",
             "sub": now[0][3] if now else (f"{label(near)} · {near['nm']:g} nm {SHORT[round(near['deg'] / 45) % 8]}" if near else "none placed") if had["n"] else "none heard",
             "says": " ".join([says(had)] + [w for _, _, w, _ in now] + ([f"{len(low)} below {LOW_FT:,} ft."] if low else [])) + " (your aircraft receiver)"}]


def _quiet() -> bool:
    try:
        return bool(QUIET())
    except Exception:                                # (not knowing is not "quiet")
        return False


def alerts(had: dict | None, spoken: bool = False) -> list[tuple[str, int, str, str]]:
    """What's worth an alert (conditions.alerts_now()): an aircraft squawking an emergency code (the DJ says it); one low
    and close, and one on the watch list (the eye only: spoken=True leaves them out, until SPEAK_EXTRAS)."""
    out = []
    for p in (had or {}).get("emergency") or []:
        sq = p["squawk"]
        out.append((f"aircraft:{p.get('hex') or label(p)}:{sq}", 2,
                    f"Aircraft {label(p)} is squawking {sq}: {EMERGENCY[sq]}." + (f" It is {where(p)}." if where(p) else ""),
                    f"{label(p)} squawking {sq}"))
    if spoken and not SPEAK_EXTRAS:
        return out
    h = had or {}
    on = lambda kind: kind not in OFF
    at = lambda p: (f" {p['nm']:g} nm {SHORT[round(p['deg'] / 45) % 8]}" if "nm" in p else "")
    key = lambda kind, p: f"aircraft:{kind}:{p.get('hex') or label(p)}"
    if on("low"):
        for p in h.get("low") or []:
            out.append((key("low", p), 1, f"Low aircraft {label(p)}: {where(p)}.", f"Low: {label(p)} {p['ft']:,} ft ·{at(p)}"))
    if on("watch"):
        for p in h.get("watch") or []:
            out.append((key("watch", p), 1, f"Watched aircraft {label(p)}" + (f": {where(p)}." if where(p) else "."), f"Watching: {label(p)}{at(p)}"))
    if on("inbound"):
        for p in h.get("inbound") or []:
            out.append((key("inbound", p), 1, f"Aircraft {label(p)} is heading for you: {where(p)}, passing in about {p['eta']:g} minutes.", f"Inbound: {label(p)} · {p['eta']:g} min"))
    if on("loiter"):
        for p in h.get("loiter") or []:
            out.append((key("loiter", p), 1, f"Aircraft {label(p)} has been circling for {p['mins']} minutes: {where(p)}.", f"Circling: {label(p)} · {p['mins']} min"))
    if on("descent"):
        for p in h.get("descent") or []:
            out.append((key("descent", p), 2, f"Aircraft {label(p)} is descending fast: {-p['rate']:,} feet a minute" + (f", {where(p)}." if where(p) else "."), f"Descending: {label(p)} · {-p['rate']:,} ft/min"))
    if on("long"):
        for p in h.get("long") or []:
            out.append((key("long", p), 1, f"Aircraft {label(p)} heard at {p['nm']:g} nautical miles: unusually far.", f"Long range: {label(p)} · {p['nm']:g} nm"))
    if on("night") and _quiet():
        for p in h.get("near") or []:
            out.append((key("night", p), 1, f"Night flight: {label(p)}, {where(p)}.", f"Night: {label(p)} · {p['ft']:,} ft ·{at(p)}"))
    if on("heli"):
        for p in h.get("heli") or []:
            out.append((key("heli", p), 1, f"Helicopter {label(p)}: {where(p)}.", f"Helicopter: {label(p)}{at(p)}"))
    if on("heavy"):
        for p in h.get("heavy") or []:
            out.append((key("heavy", p), 1, f"Heavy jet {label(p)} low: {where(p)}.", f"Heavy low: {label(p)} · {p['ft']:,} ft"))
    if on("busy") and h.get("busy"):
        b = h["busy"]
        out.append(("aircraft:busy", 1, f"A busy sky: {b['n']} aircraft, usually about {b['usual']}.", f"Busy: {b['n']} aircraft (usually {b['usual']})"))
    return out


def flagged(had: dict | None) -> list[dict]:
    """The aircraft with an alert on them (for the scope), with a position, each once: [plane + "kinds"]."""
    seen: dict[str, dict] = {}
    for what, _, _, _ in alerts(had):
        parts = what.split(":")
        if len(parts) != 3 or parts[2] in EMERGENCY:
            continue
        for p in (had or {}).get("near" if parts[1] == "night" else parts[1]) or []:
            if (p.get("hex") or label(p)) == parts[2] and "nm" in p:
                seen.setdefault(parts[2], {**p, "kinds": []})["kinds"].append(parts[1])
    return list(seen.values())
