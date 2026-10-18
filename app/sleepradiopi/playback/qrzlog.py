"""The owner's QRZ logbook: how big it is, how many countries, the latest contacts.

QRZ's Logbook API (https://logbook.qrz.com/api) is a POST of name=value pairs:
KEY (the logbook's access key, from the QRZ website: it says whose book) and
ACTION. STATUS answers with the book's figures (DATA: total QSOs, confirmed,
DXCC count, its dates...); FETCH with QSOs as ADIF. Both need a QRZ Logbook
subscription: without one, QRZ answers RESULT=AUTH or FAIL with a REASON,
and that's what's shown. The key is the owner's: kept in the radio's
settings (qrz_key, never in a backup file, never sent to the page), sent
only to QRZ. Requests carry the user agent QRZ asks for: the app, its
version, the owner's callsign.

Logbook (a conditions.Kept) asks both every EVERY_S. The desktop's "The
world outside" has a QRZ logbook tile; its window shows the figures and the
latest QSOs. With ALERTS on (conditions.py), the DJ says it when the
country count goes up ("a new country"), once.

QRZ's guide doesn't list STATUS's names, so the figures are looked for
under the likely ones (pick()) and everything it says is kept ("status")
for the window to show.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from sleepradiopi.audio import ft8
from sleepradiopi.playback.conditions import Kept

URL = "https://logbook.qrz.com/api"
EVERY_S = 1800.0
OLD_S = 12 * 3600.0
RECENT_DAYS = 30             # the latest QSOs: those modified in the last month...
RECENT_MOST = 25             # ...this many of them
NEW_FRESH_S = 6 * 3600.0     # a new country is said while it's this new
KEY = ""                     # the logbook's access key; main.py sets it from the settings (and the web side when it's changed)
TOTAL = ("QSO_COUNT", "TOTAL_QSOS", "TOTAL_QSO", "TOTAL", "COUNT")
CONFIRMED = ("CONFIRMED", "CONFIRMED_QSOS", "QSL_COUNT", "TOTAL_CONFIRMED")
DXCC = ("DXCC_COUNT", "DXCC_TOTAL", "DXCC", "TOTAL_DXCC")


class QrzRefused(ValueError):
    """QRZ answered, but no: a bad key, no subscription... (its REASON)."""


def _agent() -> str:
    from sleepradiopi.updater import this_version
    try:
        version = this_version()
    except Exception:                                # (no version file on a computer: "dev")
        version = "dev"
    return f"SleepRadio/{version} ({ft8.own_call() or 'no callsign'})"


def _post(fields: dict, timeout: float = 20.0) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(URL, data=data, headers={"User-Agent": _agent(), "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def answer(body: str) -> dict:
    """QRZ's name=value answer -> a dict. The ADIF (which may hold & and = itself) is taken whole, to the end or the
    next of QRZ's own names; DATA's pairs are read too and put alongside."""
    out: dict = {}
    m = re.search(r"(?:^|&)ADIF=(.*?)(?=&(?:RESULT|COUNT|LOGIDS|REASON|STATUS|EXTENDED)=|$)", body, re.S)
    if m:
        adif = m.group(1)
        out["ADIF"] = html.unescape(urllib.parse.unquote_plus(adif) if "%3C" in adif.upper() else adif)
        body = body[:m.start()] + body[m.end():]
    for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True):
        out[k.strip().upper()] = v
    data = out.pop("DATA", None)
    if data and "=" in data:
        for k, v in urllib.parse.parse_qsl(html.unescape(data), keep_blank_values=True):
            out.setdefault(k.strip().upper(), v)
    return out


def adif(text: str) -> list[dict]:
    """ADIF records -> [{field (lower case): value}]: <call:5>DL1AB<band:3>20m ... <eor>."""
    records, rec, i = [], {}, 0
    tag = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+)(?::[A-Za-z])?)?>")
    while (m := tag.search(text, i)):
        name, n = m.group(1).lower(), m.group(2)
        i = m.end()
        if name == "eor":
            if rec:
                records.append(rec)
            rec = {}
        elif name == "eoh":
            rec = {}
        elif n is not None:
            rec[name] = text[i:i + int(n)]
            i += int(n)
    return records


def pick(status: dict, names: tuple[str, ...]) -> int | None:
    for k in names:
        v = str(status.get(k, "")).replace(",", "").strip()
        if v.isdigit():
            return int(v)
    return None


def qso(r: dict) -> dict:
    """An ADIF record -> {"call", "t", "band", "mode", "grid"?, "km"?, "country"?}."""
    when = None
    try:
        when = datetime.strptime(r.get("qso_date", "") + r.get("time_on", "0000")[:4].ljust(4, "0"), "%Y%m%d%H%M").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    out = {"call": r.get("call", "").upper(), "t": when, "band": (r.get("band") or "").lower() or None, "mode": r.get("mode")}
    grid = (r.get("gridsquare") or "").strip()
    if grid:
        out["grid"] = grid
        if ft8.HOME:
            try:
                out["km"] = ft8.path(ft8.HOME, grid[:6])[0]
            except (ValueError, IndexError, TypeError):
                pass
    if r.get("country"):
        out["country"] = r["country"].title()
    return out


class Logbook(Kept):
    """{"total", "confirmed", "dxcc", "status": everything STATUS said, "recent": [qso()], "grew": [{"dxcc", "t"}], "t"}."""
    EVERY_S, RETRY_S, OLD_S = EVERY_S, EVERY_S, OLD_S
    NAME = "the QRZ logbook"

    def __init__(self, post: Callable = _post, clock: Callable[[], float] = time.time, key: Callable[[], str] = lambda: KEY) -> None:
        super().__init__(post, clock)
        self._key = key
        self.refused: str | None = None              # QRZ's reason, the last time it said no

    def _fetch(self) -> dict:
        key = (self._key() or "").strip()
        if not key:
            raise ValueError("no QRZ logbook key")
        st = answer(self._get({"KEY": key, "ACTION": "STATUS"}))
        if st.get("RESULT") != "OK":
            self.refused = st.get("REASON") or st.get("RESULT") or "no answer"
            raise QrzRefused(self.refused)
        self.refused = None
        status = {k: v for k, v in st.items() if k not in ("RESULT", "ADIF")}
        since = (datetime.fromtimestamp(self._clock(), timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
        recent = []
        try:
            got = answer(self._get({"KEY": key, "ACTION": "FETCH", "OPTION": f"MODSINCE:{since},MAX:{RECENT_MOST * 4},TYPE:ADIF"}))
            if got.get("RESULT") == "OK" and got.get("ADIF"):
                recent = sorted((qso(r) for r in adif(got["ADIF"])), key=lambda q: -(q["t"] or 0))[:RECENT_MOST]
        except (OSError, ValueError):
            pass                                     # (the figures without the latest: still worth having)
        had = {"total": pick(status, TOTAL), "confirmed": pick(status, CONFIRMED), "dxcc": pick(status, DXCC), "status": status, "recent": recent}
        before = self._had or {}
        grew = [g for g in before.get("grew") or [] if self._clock() - g["t"] <= NEW_FRESH_S]
        if had["dxcc"] is not None and before.get("dxcc") is not None and had["dxcc"] > before["dxcc"]:
            grew.append({"dxcc": had["dxcc"], "t": self._clock()})
        had["grew"] = grew
        return had

    def status(self) -> dict | None:
        if not (self._key() or "").strip():          # (no key, or it's been forgotten: nothing of the book shown)
            return None
        return super().status()

    def forget(self) -> None:
        """What was had, gone (a new key, or none: it was someone's book)."""
        with self._lock:
            self._had, self._tried, self.refused = None, 0.0, None
        if self._file is not None:
            try:
                self._file.unlink()
            except OSError:
                pass

    def _says(self, had: dict) -> str:
        return says(had)


logbook = Logbook()


def says(had: dict) -> str:
    parts = []
    if had.get("total") is not None:
        parts.append(f"{had['total']:,} QSOs in the logbook" + (f", {had['confirmed']:,} confirmed" if had.get("confirmed") is not None else ""))
    if had.get("dxcc") is not None:
        parts.append(f"{had['dxcc']} countries")
    if had.get("recent"):
        q = had["recent"][0]
        parts.append(f"the latest {q['call']}" + (f" on {q['band']}" if q.get("band") else "") + (f" ({q['mode']})" if q.get("mode") else ""))
    if not parts:
        return "The logbook answered, but with no figures in it."
    said = ", ".join(parts) + "."
    return said[0].upper() + said[1:]


def alerts(had: dict | None, now: float | None = None) -> list[tuple[str, int, str, str]]:
    """A new country, while it's new (conditions.alerts_now())."""
    now = time.time() if now is None else now
    return [(f"qrz:dxcc:{g['dxcc']}", 1, f"A new country in your QRZ logbook: {g['dxcc']} now.", f"New country: {g['dxcc']} in the logbook")
            for g in (had or {}).get("grew") or [] if now - g["t"] <= NEW_FRESH_S]


def tile(had: dict | None, alerted: set[str]) -> list[dict]:
    """The QRZ logbook tile for "The world outside" (conditions.outside()). It's there with no key too, so its
    window (where the key goes in) can be found."""
    if not had:
        refused = logbook.refused if KEY else None
        return [{"key": "qrz", "title": "QRZ logbook", "value": "no" if refused else "…" if KEY else "key?", "state": "quiet",
                 "sub": "QRZ said no" if refused else "asking QRZ" if KEY else "add your access key",
                 "says": f"QRZ said no: {refused}." if refused else "Waiting for QRZ." if KEY else "Open this to add your QRZ logbook's access key."}]
    q = (had.get("recent") or [None])[0]
    sub = " · ".join(x for x in (f"{had['dxcc']} countries" if had.get("dxcc") is not None else "",
                                 f"last {q['call']}" + (f" {q['band']}" if q.get("band") else "") if q else "") if x) or "QRZ"
    return [{"key": "qrz", "title": "QRZ logbook", "value": f"{had['total']:,}" if had.get("total") is not None else "QRZ",
             "state": "alert" if "qrz" in alerted else "good" if had.get("recent") else "quiet", "sub": sub, "says": says(had) + " (QRZ)"}]
