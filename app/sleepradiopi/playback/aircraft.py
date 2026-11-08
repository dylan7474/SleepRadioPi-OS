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
import time
import urllib.parse
import urllib.request
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept
from sleepradiopi.playback.lightning import compass

log = logging.getLogger(__name__)

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
    sq = str(a.get("squawk") or "")
    if sq in EMERGENCY:
        out["squawk"] = sq
    if at and isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float)):
        nm, deg = distance(at[0], at[1], a["lat"], a["lon"])
        out["nm"], out["deg"] = round(nm, 1), round(deg)
    return out if len(out) > 1 else None


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
        return {"n": len(raw), "placed": len(placed), "planes": placed[:NEAREST] + rest[:LIST_MOST], "emergency": emergency,
                "low": low, "watch": watch, "far_nm": placed[-1]["nm"] if placed else None, "aerial": bool(at), "from": src}

    def fetch(self) -> dict | None:
        had = super().fetch()
        if had is None:
            self._empty_since = None                 # (it didn't answer: nothing is known about what it hears)
        return had

    def trouble(self) -> dict | None:
        """Something for the "something's wrong" light: a receiver that answers but has heard no aircraft at all for DEAF_S."""
        since = self._empty_since
        if since is None or not self.wanted() or self._clock() - since < DEAF_S:
            return None
        return {"key": "aircraft-deaf", "level": "warn", "title": "Aircraft receiver hears nothing",
                "text": f"The aircraft receiver answers but has heard no aircraft for {int((self._clock() - since) // 3600)} h or more: check its aerial and dongle."}

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


def alerts(had: dict | None, spoken: bool = False) -> list[tuple[str, int, str, str]]:
    """What's worth an alert (conditions.alerts_now()): an aircraft squawking an emergency code (the DJ says it); one low
    and close, and one on the watch list (the eye only: spoken=True leaves them out, until SPEAK_EXTRAS)."""
    out = []
    for p in (had or {}).get("emergency") or []:
        sq = p["squawk"]
        out.append((f"aircraft:{p.get('hex') or label(p)}:{sq}", 2,
                    f"Aircraft {label(p)} is squawking {sq}: {EMERGENCY[sq]}." + (f" It is {where(p)}." if where(p) else ""),
                    f"{label(p)} squawking {sq}"))
    if not spoken or SPEAK_EXTRAS:
        for p in (had or {}).get("low") or []:
            out.append((f"aircraft:low:{p.get('hex') or label(p)}", 1, f"Low aircraft {label(p)}: {where(p)}.",
                        f"Low: {label(p)} {p['ft']:,} ft · {p['nm']:g} nm {SHORT[round(p['deg'] / 45) % 8]}"))
        for p in (had or {}).get("watch") or []:
            out.append((f"aircraft:watch:{p.get('hex') or label(p)}", 1, f"Watched aircraft {label(p)}" + (f": {where(p)}." if where(p) else "."),
                        f"Watching: {label(p)}" + (f" {p['nm']:g} nm {SHORT[round(p['deg'] / 45) % 8]}" if "nm" in p else "")))
    return out
