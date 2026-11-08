"""433 MHz devices heard, from the receiver's background service on the local network.

The receiver Pi (receiver/sleepradio_background.py, port 8076) listens on 433.92 MHz with rtl_433 whenever nobody is
using its dongle, and serves what it hears as JSON: <address>/433 (every device: a model, a made-up ID, a count, a
signal strength, everything it says -- temperature, humidity, battery... -- and the name its owner gave it there, if any) and <address>/status
(whether it's scanning, or paused because the VHF receiver is in use, or listening for radiosondes). The owner types
its address into the 433 MHz window (the setting background_url: "192.168.50.4:8076"; empty = off, and the address
is never in the source). Only that address is asked, and it is the radio that asks: the receiver never sends.

It's a LAN thing, so it comes and goes: Radio433 (a conditions.Kept) asks every EVERY_S and keeps NOTHING on the
card -- the history (messages per quarter hour for a day, and the readings of the named devices) is in memory and
begins again when the radio does. The tile and window exist only while the receiver answers (gone OLD_S after it
stops), and it is kept out of the "something's wrong" light's stale-feed list (LAN).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sleepradiopi.playback.aircraft import _get, base
from sleepradiopi.playback.conditions import Kept

log = logging.getLogger(__name__)

ADDRESS = ""                 # the background service's address; main.py sets it from the settings (and the web side when it's changed)
EVERY_S = 20.0
OLD_S = 120.0                # a picture this old is no picture: the tile goes
BUCKET_S = 900.0             # the activity is kept in quarter hours...
HIST_S = 86400.0             # ...for a day
SERIES_S = 300.0             # a named device's readings: one every five minutes...
SERIES_MAX = 288             # ...for a day
SERIES_DEVICES = 24          # ...for this many devices at most (the ones that report a temperature)
MAX_FIELDS = 14
MOST = 60                    # devices served (the most recently heard first; the receiver keeps up to 500)
HOUR_S = 3600
NOW_WORDS = {"scan433": "Scanning", "sondes": "Paused: listening for radiosondes"}


def clean(d: dict) -> dict | None:
    """One device from the service -> what is kept (None if it isn't one). ago_s is as of the look; status() adds the time since."""
    if not isinstance(d, dict) or not isinstance(d.get("model"), str) or not isinstance(d.get("id"), str):
        return None
    out = {"id": d["id"][:16], "model": d["model"][:60], "count": int(d.get("count") or 0), "ago_s": max(0, round(float(d.get("ago_s") or 0)))}
    for k in ("rssi", "snr", "freq"):
        if isinstance(d.get(k), (int, float)):
            out[k] = d[k]
    if isinstance(d.get("name"), str) and d["name"]:
        out["name"] = d["name"][:40]
    if isinstance(d.get("real"), str):
        out["real"] = d["real"][:60]                  # (what rtl_433 calls it: "Acurite-Tower:1234", the key for a name in known.json; not sent for a car's tyre sensors)
    r = {k[:40]: (v[:24] if isinstance(v, str) else v) for k, v in (d.get("readings") or {}).items()
         if isinstance(k, str) and ((isinstance(v, (int, float)) and not isinstance(v, bool)) or isinstance(v, str))}
    if r:
        out["readings"] = dict(list(r.items())[:MAX_FIELDS])
    return out


def state_word(had: dict) -> str:
    """What the dongle is doing, in words (the window and the tile say it; never colour alone)."""
    job = had.get("job")
    if job in NOW_WORDS:
        return NOW_WORDS[job]
    return "Paused: the VHF receiver is in use" if had.get("receiver_busy") else "Starting again"


class Radio433(Kept):
    """{"job": scan433 | sondes | None, "receiver_busy", "windows": the radiosonde hours (UTC), "messages", "devices": [...], "activity": [[t, n]...] a day in
    quarter hours, "series": {name: [[t, temperature, humidity]...]}, "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the 433 MHz scan"
    LAN = True               # (a local-network thing: not asked for in the "something's wrong" light when it's not there)

    def __init__(self, get: Callable = _get, clock: Callable[[], float] = time.time, address: Callable[[], str] = lambda: ADDRESS) -> None:
        super().__init__(get, clock)
        self._address = address
        self._mem = threading.Lock()
        self._activity: dict[int, int] = {}          # quarter hour -> messages heard in it (by this radio's watching; memory only)
        self._series: dict[str, list] = {}           # a device (its made-up ID) that reports a temperature -> [(t, temperature, humidity)]
        self._total: int | None = None               # the service's message count at the last look (a smaller one: it restarted)

    def _fetch(self) -> dict:
        url = base(self._address())
        if not url:
            raise ValueError("no address")
        scan, status = self._get(url + "/433"), self._get(url + "/status")
        if not isinstance(scan.get("devices"), list):
            raise ValueError("no device list")
        now = self._clock()
        devices = [d for d in (clean(x) for x in scan["devices"]) if d]
        devices.sort(key=lambda d: d["ago_s"])
        total = int(scan.get("messages") or 0)
        with self._mem:
            delta = total - self._total if self._total is not None and total >= self._total else 0
            self._total = total
            b = int(now // BUCKET_S)
            self._activity[b] = self._activity.get(b, 0) + delta
            for k in [k for k in self._activity if k < b - HIST_S / BUCKET_S]:
                del self._activity[k]
            for d in devices:
                r = d.get("readings") or {}
                if d["ago_s"] < OLD_S * 3 and isinstance(r.get("temperature_C"), (int, float)):
                    if d["id"] not in self._series and len(self._series) >= SERIES_DEVICES:
                        del self._series[min(self._series, key=lambda k: self._series[k][-1][0])]
                    s = self._series.setdefault(d["id"], [])
                    if not s or now - s[-1][0] >= SERIES_S:
                        s.append((round(now), r["temperature_C"], r.get("humidity")))
                        del s[:-SERIES_MAX]
            first = b - int(HIST_S / BUCKET_S) + 1
            activity = [[(first + i) * BUCKET_S, self._activity.get(first + i, 0)] for i in range(int(HIST_S / BUCKET_S))]
            series = {k: [list(p) for p in v] for k, v in self._series.items()}
        job = status.get("job") if isinstance(status, dict) else None
        return {"job": job if isinstance(job, str) else None, "receiver_busy": bool(status.get("receiver_busy")),
                "windows": [w for w in status.get("sonde_windows_utc") or [] if isinstance(w, str)][:6],
                "messages": total, "devices": devices[:MOST], "activity": activity, "series": series}

    def wanted(self) -> bool:
        return base(self._address()) is not None

    def refresh(self) -> None:
        if self.wanted():                            # (no address: nothing to ask)
            super().refresh()

    def status(self, refresh: bool = True, old_ok: bool = False) -> dict | None:
        if not self.wanted():
            return None
        had = super().status(refresh, False)         # (an old picture of the air is no use: never "old", it just goes)
        if had is None:
            return None
        gone = self._clock() - had["t"]              # (as of now: what was heard 20 s ago was heard that much longer ago)
        return {**had, "devices": [{**d, "ago_s": d["ago_s"] + round(gone)} for d in had["devices"]]}

    def forget(self) -> None:
        """What was had, gone (a new address, or none)."""
        with self._lock:
            self._had, self._tried = None, 0.0
        with self._mem:
            self._activity.clear(), self._series.clear()
            self._total = None

    def _says(self, had: dict) -> str:
        n, hour = len(had["devices"]), sum(1 for d in had["devices"] if d["ago_s"] < HOUR_S)
        return f"{state_word(had)}. {hour} device{'s' if hour != 1 else ''} heard in the last hour, {n} since it began ({had['messages']:,} messages)."


def reading(d: dict) -> str:
    """A device's readings in a few words ("7.5°C · 80%"): its temperature, humidity and pressure, when it says them."""
    r = d.get("readings") or {}
    parts = []
    if isinstance(r.get("temperature_C"), (int, float)):
        parts.append(f"{r['temperature_C']:g}°C")
    if isinstance(r.get("humidity"), (int, float)):
        parts.append(f"{r['humidity']:g}%")
    if isinstance(r.get("pressure_kPa"), (int, float)):
        parts.append(f"{r['pressure_kPa']:g} kPa")
    elif isinstance(r.get("pressure_hPa"), (int, float)):
        parts.append(f"{r['pressure_hPa']:g} hPa")
    return " · ".join(parts)


def label(d: dict) -> str:
    return d.get("name") or d["model"]


def tile(had: dict | None, alerted: set[str]) -> list[dict]:
    """The 433 MHz tile for "The world outside" (conditions.outside()): there only while the receiver answers."""
    if not had:
        return []
    recent = [d for d in had["devices"] if d["ago_s"] < HOUR_S]
    named = [d for d in recent if reading(d)]
    named.sort(key=lambda d: (not d.get("name"), d["ago_s"]))             # (yours first, then the latest heard)
    paused = had.get("job") != "scan433"
    sub = (f"{label(named[0])} {reading(named[0])}" if named else f"{len(recent)} device{'s' if len(recent) != 1 else ''} in the last hour") if (named or recent) else "nothing heard yet"
    if paused:
        sub = state_word(had).replace("Paused: ", "paused: ").replace("Starting again", "starting again") + (f" · {sub}" if recent else "")
    return [{"key": "r433", "title": "433 MHz", "value": str(len(recent)), "state": "good" if recent else "quiet", "sub": sub,
             "says": " ".join([state_word(had) + "."] + [f"{label(d)}: {reading(d)}." for d in named[:3]] +
                              [f"{len(recent)} device{'s' if len(recent) != 1 else ''} heard in the last hour ({had['messages']:,} messages since the scan began)."]) + " (your receiver's 433 MHz scan)"}]


radio433 = Radio433()
