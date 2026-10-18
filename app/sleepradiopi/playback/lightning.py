"""Lightning near here, from Blitzortung's live feed of strikes.

Blitzortung.org is a community network of lightning receivers; its live map
gets every strike, worldwide, over a WebSocket: connect to one of its
servers, send {"a":111}, and each message is a strike -- {"lat", "lon",
"time" (ns), "pol", ...} -- squeezed by a small LZW scheme (decode(), as the
site's own script; the owner's own terminal map, github.com/dylan7474/
lightning, reads it the same way). There's no key and no way to ask for one
area: the world comes, four strikes a second on a quiet evening, many more
on a summer afternoon. So the radio keeps only those within AREA_KM of its
locator, and listens in short spells (SAMPLE_S every EVERY_S) -- a storm
near here strikes every few seconds for tens of minutes, so a spell catches
it -- staying on while strikes keep landing near (until STAY_S passes
without one). Blitzortung keeps its raw feed for those who run a receiver,
and its data isn't for protecting life or property: here it's a curiosity,
and it may stop working without notice.

Watch is that listener: a thread of its own (start()), switched by ON (the
setting lightning_watch), never with no locator. The desktop's "The world
outside" has a Lightning tile, the nearest in the last hour on its face;
the Lightning window lists them. With ALERTS on (conditions.py), the DJ says
it when a storm comes within ALERT_KM: once a storm. And with BELL on, a
bell rings (on_bell: main.py plays BELL_FILE, a real bell, unless it's the
quiet hours) when a storm first comes within ALERT_KM, and again if one
strikes within CLOSE_KM: twice a storm at most.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sleepradiopi.audio import ft8
from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

HOSTS = ("ws1.blitzortung.org", "ws7.blitzortung.org")
SAMPLE_S = 30.0              # a spell's length...
EVERY_S = 600.0              # ...this often
STAY_S = 1200.0              # with a strike near, listening goes on until this long without one
AREA_KM = 100                # strikes kept: this near
ALERT_KM = 40                # the DJ says it: this near
KEEP_S = 3 * 3600.0          # strikes kept this long
HOUR_S = 3600.0
SPELL_GAP_S = 3600.0         # a strike this long after the last near one is a new storm
FRESH_S = 600.0              # what's said as now: the last ten minutes
MOST = 500
ON = True                    # main.py sets it from the settings (lightning_watch)
BELL = True                  # ...and this (lightning_bell)
CLOSE_KM = 10                # the bell rings again: this near
BELL_FILE = Path(__file__).resolve().parent.parent / "data" / "lightning_bell.wav"
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def decode(s: str) -> str:
    """A Blitzortung message -> its JSON: the LZW its live map uses (codes over 255 are earlier runs)."""
    if not s:
        return s
    d: dict[int, str] = {}
    f = c = s[0]
    out, code = [c], 256
    for ch in s[1:]:
        n = ord(ch)
        a = ch if n < 256 else d.get(n, f + c)
        out.append(a)
        c = a[0]
        d[code] = f + c
        code += 1
        f = a
    return "".join(out)


def where(lat: float, lon: float, home: str | None = None) -> tuple[int, int] | None:
    """(km, bearing in degrees) from the radio's locator, or None with no locator."""
    home = ft8.HOME if home is None else home
    try:
        hlat, hlon = ft8.locate(home)
    except (ValueError, IndexError, TypeError, AttributeError):
        return None
    p1, p2, dl = math.radians(hlat), math.radians(lat), math.radians(lon - hlon)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * 6371 * math.asin(min(1.0, math.sqrt(a)))
    deg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return round(km), round(deg) % 360


def compass(deg: float) -> str:
    return COMPASS[round(deg / 45) % 8]


def _connect(host: str):
    from websockets.sync.client import connect        # (python-websockets: in the image; a desktop may lack it)
    return connect(f"wss://{host}/", open_timeout=10, user_agent_header="SleepRadio", max_size=1_000_000)


class Watch:
    """Listens now and then; keeps the strikes near here. status() for the page, the tile and the alerts."""

    def __init__(self, connect: Callable = _connect, clock: Callable[[], float] = time.time) -> None:
        self._connect, self._clock = connect, clock
        self._lock = threading.Lock()
        self._strikes: list[dict] = []               # {"t", "km", "deg", "lat", "lon"}, the latest last
        self._file: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._host = 0
        self.listening = False
        self.on_bell: Callable[[], None] | None = None   # main.py: rings the bell (unless it's the quiet hours)
        self._rung: set[str] = set()                 # what's been rung for: "storm:<when it began>", "close:<...>"
        self.listened: float | None = None           # when a spell last ended
        self.heard = 0                               # strikes in the world in the last spell (to show it's alive)
        self.error: str | None = None

    def keep_in(self, file: Path | None) -> None:
        self._file = file
        try:
            got = json.loads(file.read_text()) if file else []
            if isinstance(got, list):
                self._strikes = [s for s in got if isinstance(s, dict) and isinstance(s.get("t"), (int, float))]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if self._file is not None:
            try:
                write_atomic(self._file, json.dumps(self._strikes))
            except OSError:
                pass

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lightning", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            began = self._clock()
            if ON and ft8.HOME:
                try:
                    self.listen()
                except Exception:                    # (nothing of the lightning may stop the radio)
                    log.exception("lightning: the spell failed")
            self._stop.wait(max(5.0, EVERY_S - (self._clock() - began)))

    def listen(self, spell: float = SAMPLE_S) -> int:
        """One spell: listen for `spell` seconds -- longer while strikes keep landing near. -> how many near."""
        host = HOSTS[self._host % len(HOSTS)]
        near, heard = 0, 0
        self.listening = True
        try:
            with self._connect(host) as ws:
                ws.send('{"a":111}')
                until = self._clock() + spell
                while self._clock() < until and not self._stop.is_set() and ON:
                    try:
                        msg = ws.recv(timeout=5)
                    except TimeoutError:
                        continue
                    heard += 1
                    s = self.take(msg)
                    if s:
                        near += 1
                        until = max(until, self._clock() + STAY_S)     # a storm near: stay on it
            self.error = None
        except Exception as e:                       # (no internet, a server down: the other next time)
            self._host += 1
            self.error = f"{host}: {type(e).__name__}"
            log.info("lightning: %s", self.error)
        finally:
            self.listening = False
            self.listened, self.heard = self._clock(), heard
            self._save()
        return near

    def take(self, msg) -> dict | None:
        """A message from the feed -> the strike, kept, if it's near here (None if not, or not a strike)."""
        try:
            j = json.loads(decode(msg) if isinstance(msg, str) else msg.decode("utf-8", "replace"))
            lat, lon = float(j["lat"]), float(j["lon"])
            t = float(j.get("time") or 0) / 1e9 or self._clock()
        except (ValueError, TypeError, KeyError):
            return None
        w = where(lat, lon)
        if w is None or w[0] > AREA_KM:
            return None
        s = {"t": t, "km": w[0], "deg": w[1], "lat": round(lat, 3), "lon": round(lon, 3)}
        with self._lock:
            now = self._clock()
            self._strikes = [x for x in self._strikes if now - x["t"] <= KEEP_S][-(MOST - 1):] + [s]
        self._maybe_ring(s)
        return s

    def _maybe_ring(self, s: dict) -> None:
        """The bell, for a storm come within ALERT_KM, and again within CLOSE_KM: once each a storm."""
        if not BELL or self.on_bell is None or s["km"] > ALERT_KM:
            return
        storm = alerts(self.status())
        if not storm:
            return
        began = storm[0][0].split(":", 1)[1]
        why = f"close:{began}" if s["km"] <= CLOSE_KM else f"storm:{began}"
        if why in self._rung or (why.startswith("storm:") and f"close:{began}" in self._rung):
            return
        self._rung = {k for k in self._rung if k.split(":", 1)[1] == began} | {why}     # (only this storm's kept)
        threading.Thread(target=self._ring, name="lightning-bell", daemon=True).start()

    def _ring(self) -> None:
        try:
            self.on_bell()
        except Exception:                            # (a bell that can't ring mustn't stop the listening)
            log.exception("lightning: the bell")

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            strikes = [x for x in self._strikes if now - x["t"] <= KEEP_S]
        return {"on": ON, "bell": BELL, "close_km": CLOSE_KM, "home": ft8.HOME, "area_km": AREA_KM, "alert_km": ALERT_KM, "listening": self.listening,
                "listened": self.listened, "heard": self.heard, "error": self.error, "every_s": EVERY_S, "sample_s": SAMPLE_S,
                "strikes": sorted(strikes, key=lambda x: -x["t"]), "t": now}


watch = Watch()


def bell(path: Path = BELL_FILE) -> np.ndarray:
    """The bell: BELL_FILE (a 16-bit WAV at the radio's rate) as int16 stereo."""
    from sleepradiopi.audio import pcm
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getframerate() != pcm.SAMPLE_RATE:
            raise ValueError(f"{path.name}: wants 16-bit, {pcm.SAMPLE_RATE} Hz")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    return np.repeat(x[:, :1], pcm.CHANNELS, axis=1) if x.shape[1] != pcm.CHANNELS else x.copy()


def hour(st: dict | None, now: float | None = None) -> list[dict]:
    if not st:
        return []
    now = st.get("t", time.time()) if now is None else now
    return [s for s in st.get("strikes") or [] if now - s["t"] <= HOUR_S]


def says(st: dict, now: float | None = None) -> str:
    h = hour(st, now)
    if not h:
        return f"No lightning within {st.get('area_km', AREA_KM)} km in the last hour."
    near = min(h, key=lambda s: s["km"])
    return (f"{len(h)} strike{'' if len(h) == 1 else 's'} within {st.get('area_km', AREA_KM)} km in the last hour; "
            f"the nearest {near['km']} km to the {compass(near['deg'])}.")


def alerts(st: dict | None, now: float | None = None) -> list[tuple[str, int, str, str]]:
    """A storm come within ALERT_KM in the last FRESH_S (conditions.alerts_now()): once a storm -- the key is when
    its spell of near strikes began. Closer than in the ten minutes before: "getting closer"."""
    if not st:
        return []
    now = st.get("t", time.time()) if now is None else now
    strikes = sorted(st.get("strikes") or [], key=lambda s: s["t"])
    fresh = [s for s in strikes if now - s["t"] <= FRESH_S and s["km"] <= ALERT_KM]
    if not fresh:
        return []
    began = fresh[0]["t"]
    for s in reversed([s for s in strikes if s["t"] < began]):      # back to the start of this spell of near strikes
        if began - s["t"] > SPELL_GAP_S:
            break
        began = s["t"]
    near = min(fresh, key=lambda s: s["km"])
    before = [s["km"] for s in strikes if FRESH_S < now - s["t"] <= 2 * FRESH_S]
    closer = bool(before) and near["km"] < min(before) - 5
    km_words = f"{near['km']} kilometre{'' if near['km'] == 1 else 's'}"
    return [(f"lightning:{round(began)}", 1, f"Lightning {km_words} to the {compass(near['deg'])}" + (", and getting closer." if closer else "."),
             f"Lightning {near['km']} km {compass(near['deg'])}")]


def tile(st: dict | None, alerted: set[str], now: float | None = None) -> list[dict]:
    """The Lightning tile for "The world outside" (conditions.outside()): the nearest in the last hour."""
    if not st or not st.get("on") or not st.get("home") or (st.get("listened") is None and not st.get("strikes")):
        return []                                    # (not until it has listened: a radio with no internet has no tile)
    h = hour(st, now)
    near = min(h, key=lambda s: s["km"]) if h else None
    return [{"key": "lightning", "title": "Lightning", "value": f"{near['km']} km" if near else "none",
             "state": "alert" if "lightning" in alerted else "quiet" if not near else "poor" if near["km"] <= ALERT_KM else "fair",
             "sub": f"{len(h)} in the hour · {compass(near['deg'])}" if near else f"none within {st.get('area_km', AREA_KM)} km",
             "says": says(st, now) + " (Blitzortung.org)"}]
