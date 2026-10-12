"""The survey, run by the radio: a receiver of your own taken round the bands, and what's on each written down.

"Where to listen" (the Modes… key) has a key for each mode's place on each
band, and a survey that goes round them and writes on the keys what it
heard. The page used to do the going round, through the radio's speaker:
close the page and it stopped. For a receiver of the owner's own the radio
does it itself, here:

  * The receiver's sound is read straight from it (its /audio, as
    playback/calibrate.py does), not through the speaker; if the radio is
    playing that receiver, the round is heard all the same, because the
    receiver hears one thing at a time.
  * At each place it's tuned there (its /tune) and listened to:
      FT8, FT4, WSPR   one whole slot by the clock, read by the same
                       decoders as the reader's (audio/ft8.py): how many
                       stations, and the farthest if the radio knows its
                       own locator;
      PSK31            a few seconds: how many signals stand out;
      weather          the German weather service's teleprinters: there or
                       not (the two tones, as calibrate.weather finds them).
  * It's put back where it was at the end.

What was found is kept (keep_in(): on the card, written once at the end of
a round), a receiver at a time and a place at a time: the latest, and each
finding of the last day, so a key can show a band's day -- when it opened,
when it was busiest.

By itself: the Rounds does a receiver's survey on the hour, every so many
hours, for the receivers the owner has chosen, with the places they chose
-- but only while the radio isn't playing that receiver (nobody hears it
go), nothing else has it (a calibration), and nobody else is listening to
it: another radio in the house may be, and a round begun by itself leaves
a receiver with a listener alone.

One thing at a time: a survey and a calibration never run together.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from sleepradiopi.audio import ft8, psk
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback import calibrate

log = logging.getLogger(__name__)

AUDIO_RATE = calibrate.AUDIO_RATE
MODES = ("ft8", "ft4", "wspr", "psk31", "weather")
SLOT_MODES = ("ft8", "ft4", "wspr")
CUTS_S = {"ft8": (0.5, 2.0, 3.5), "ft4": (0.5, 2.0, 3.5), "wspr": (2.0,)}     # how late a slot is cut, each tried (see audio/ft8.py)
BEFORE_S, AFTER_S = 1.0, 4.5         # listened from this long before a slot begins to this long after it ends
SETTLE_S = 1.2                       # the receiver moving
PSK_LISTEN_S, WEATHER_LISTEN_S = 8.0, 4.0
PLACES_MOST = 120
DAY_S = 24 * 3600.0
DAY_MOST = 30                        # findings kept for a place's day
EVERY_H = (1, 2, 3, 6)               # by itself: how many hours apart
TICK_S = 30.0


def clean_places(places) -> list[dict]:
    """The places to survey, as the page sends them: [{"key", "mode", "band", "hz"}]. ValueError says what's wrong."""
    if not isinstance(places, list) or not places or len(places) > PLACES_MOST:
        raise ValueError(f"places is a list of where to listen, {PLACES_MOST} at most")
    out, seen = [], set()
    for p in places:
        if not isinstance(p, dict) or p.get("mode") not in MODES:
            raise ValueError("a place has a mode: " + ", ".join(MODES))
        hz, key, band = p.get("hz"), p.get("key"), p.get("band")
        if isinstance(hz, bool) or not isinstance(hz, (int, float)) or not 100_000 <= hz <= 500_000_000:
            raise ValueError("a place has a frequency, in Hz")
        if not isinstance(key, str) or not 1 <= len(key) <= 40 or not isinstance(band, str) or len(band) > 20:
            raise ValueError("a place has a key and a band, both short")
        if key in seen:
            raise ValueError("each place has a key of its own")
        seen.add(key)
        out.append({"key": key, "mode": p["mode"], "band": band, "hz": int(hz)})
    return out


def clean_base(base) -> str:
    base = str(base).rstrip("/")
    if not base.startswith(("http://", "https://")) or any(c.isspace() for c in base):
        raise ValueError("base is the receiver's address: http://192.168.50.154:8074")
    return base


def slot_heard(x: np.ndarray, began: float, t0: float, mode: str, decode: Callable) -> dict:
    """The slot that began at t0 by the clock, in sound at 12 kHz that began at `began` -> {"n": stations heard,
    "km"?: the farthest}: cut each of a few ways late, the way that reads most."""
    used = ft8.USED_S.get(mode, ft8.MODES[mode])
    best: list[dict] = []
    for late in CUTS_S[mode]:
        a = int(round((t0 + late - began) * AUDIO_RATE))
        got = x[max(a, 0):max(a + int(used * AUDIO_RATE), 0)]
        if len(got) < ft8.ENOUGH * used * AUDIO_RATE:
            continue
        try:
            read = decode(np.clip(got, -32768, 32767).astype(np.int16), mode)
        except RuntimeError as e:
            log.info("survey: %s", e)
            continue
        if len(read) > len(best):
            best = read
    far = 0
    for m in best:
        call, grid = (m.get("call"), m.get("grid")) if mode == "wspr" else ft8.who(m["text"])[:2]
        if call and grid and ft8.HOME:
            far = max(far, ft8.path(ft8.HOME, grid)[0])
    return {"n": len(best), **({"km": far} if far else {})}


class Job:
    """One round of one receiver, in a thread of its own: start(base, places), then status() until it's done."""

    def __init__(self, get: Callable = calibrate._get, post: Callable = calibrate._post, sound: Callable = calibrate._Sound,
                 decode: Callable | None = None, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self._get, self._post, self._sound = get, post, sound
        self._decode = decode or ft8.run_decoder
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._state: dict = {"running": False}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._found: dict[str, dict[str, dict]] = {}     # receiver -> place's key -> {"n", "km"?, "t", "day": [[t, n]]}
        self._file: Path | None = None

    # --- what's been found, kept -------------------------------------------------------------

    def keep_in(self, file: Path | None) -> None:
        """Where what's found is kept from now on, and what's there read back in."""
        self._file = file
        if file is None:
            return
        try:
            kept = json.loads(file.read_text())["found"]
            found = {str(base): {str(key): {"n": int(f["n"]), "t": float(f["t"]), **({"km": int(f["km"])} if f.get("km") else {}),
                                            "day": [[float(t), int(n)] for t, n in f.get("day", [])][-DAY_MOST:]}
                                 for key, f in places.items()} for base, places in kept.items()}
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("survey: what was found, in %s, can't be read (%s): beginning again", file, e)
            return
        with self._lock:
            self._found = found

    def _save(self) -> None:
        with self._lock:
            if self._file is None:
                return
            text = json.dumps({"found": self._found}, separators=(",", ":"))
        try:
            write_atomic(self._file, text)
        except OSError as e:
            log.warning("survey: what was found wasn't kept (%s)", e)

    def found(self, base: str) -> dict:
        """What's been found on a receiver: {place's key: {"n", "km"?, "t" (when, by the clock), "day": [[t, n]] (each
        finding of the last day, the oldest first)}}"""
        now = self._clock()
        with self._lock:
            return {key: {**f, "day": [d for d in f["day"] if now - d[0] <= DAY_S]} for key, f in self._found.get(base, {}).items()}

    def forget(self, base: str) -> None:
        with self._lock:
            self._found.pop(base, None)
        self._save()

    # --- the round ---------------------------------------------------------------------------

    def status(self) -> dict:
        """{"running", "base", "at" (the key of the place being listened to), "i", "of", "why" ("asked" | "itself"),
        then "done": how many places, or "error"}"""
        with self._lock:
            return dict(self._state)

    def start(self, base: str, places: list[dict], why: str = "asked") -> None:
        """ValueError if a survey or a calibration is running."""
        with self._lock:
            if self._state.get("running"):
                raise ValueError("a survey is already running")
            if calibrate.job.status().get("running"):
                raise ValueError("a receiver is being calibrated: a survey can't run until that's done")
            self._stop = threading.Event()
            self._state = {"running": True, "base": base, "at": None, "i": 0, "of": len(places), "why": why}
            self._thread = threading.Thread(target=self._run, args=(base, list(places), self._stop, why), name="survey", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _wait(self, seconds: float, stop: threading.Event) -> bool:
        """Wait, in short steps. False: stopped."""
        until = self._clock() + seconds
        while self._clock() < until:
            if stop.is_set():
                return False
            self._sleep(min(0.2, max(0.0, until - self._clock())))
        return not stop.is_set()

    def _listen(self, place: dict, sound, stop: threading.Event) -> dict | None:
        """What's at one place, the receiver being there already. None: stopped."""
        mode = place["mode"]
        if mode in SLOT_MODES:
            slot = ft8.MODES[mode]
            now = self._clock()
            t0 = float(np.ceil((now + SETTLE_S + BEFORE_S) / slot) * slot)      # the next slot to begin
            if not self._wait(t0 - BEFORE_S - now, stop):
                return None
            sound.mark()
            began = sound.began
            if not self._wait(BEFORE_S + ft8.USED_S.get(mode, slot) + AFTER_S, stop):
                return None
            return slot_heard(sound.since_mark(), began, t0, mode, self._decode)
        if not self._wait(SETTLE_S, stop):
            return None
        sound.mark()
        if not self._wait(PSK_LISTEN_S if mode == "psk31" else WEATHER_LISTEN_S, stop):
            return None
        x = sound.since_mark()
        if mode == "psk31":
            return {"n": len(psk.find(x, AUDIO_RATE)) if len(x) >= AUDIO_RATE else 0}
        return {"n": 1 if len(x) >= AUDIO_RATE and calibrate.weather(x) is not None else 0}

    def _run(self, base: str, places: list[dict], stop: threading.Event, why: str = "asked") -> None:
        sound, done, before, moved = None, 0, None, False
        try:
            st = self._get(base + "/status")
            if not st.get("tunable"):                # (a receiver that's tuned like a rig says how far it reaches)
                raise ValueError("this receiver isn't one the radio can tune: a survey can't move it")
            before = st.get("receiving") if st.get("tunes") else None      # (it says "tunes" only while someone is tuning it)
            if why == "itself" and st.get("listeners"):      # (another radio, or a browser: not ours to take away unasked)
                raise ValueError("somebody is listening to it: left alone this time")
            lo, hi = st["tunable"]
            first = next((p for p in places if lo <= p["hz"] <= hi), None)
            if first is None:
                raise ValueError("this receiver doesn't reach any of those places")
            # (someone listening and tuning it: joined as it is, then moved; nobody: started)
            sound = self._sound(base + ("/audio" if before else f"/audio?freq={first['hz'] / 1e6:.4f}&mode=usb"))
            for i, place in enumerate(places):
                if stop.is_set():
                    break
                with self._lock:
                    self._state.update(at=place["key"], i=i + 1)
                if not lo <= place["hz"] <= hi:
                    continue
                moved = True
                self._post(base + "/tune", {"freq": place["hz"], "mode": "usb"})
                heard = self._listen(place, sound, stop)
                if heard is None:
                    break
                t = self._clock()
                with self._lock:
                    was = self._found.setdefault(base, {}).get(place["key"], {})
                    day = [d for d in was.get("day", []) if t - d[0] <= DAY_S] + [[round(t), heard["n"]]]
                    self._found[base][place["key"]] = {**heard, "t": round(t), "day": day[-DAY_MOST:]}
                done += 1
            with self._lock:
                self._state.update(running=False, at=None, done=done, **({"stopped": True} if stop.is_set() else {}))
        except (ValueError, OSError, KeyError, TypeError) as e:
            log.warning("survey: %s", e)
            with self._lock:
                self._state.update(running=False, at=None, done=done, error=str(e) if isinstance(e, ValueError) else f"the receiver isn't answering ({e})")
        finally:
            if before and moved:                     # back where it was
                try:
                    self._post(base + "/tune", {"freq": before["freq"], "mode": before["mode"]})
                except (ValueError, OSError, KeyError, TypeError):
                    pass
            if sound is not None:
                sound.close()
            if done:
                self._save()


def clean_plan(plan) -> dict[str, dict]:
    """The receivers surveyed by themselves, as kept: {receiver: {"every_h": 1 | 2 | 3 | 6, "places": [...]}}.
    ValueError says what's wrong."""
    if not isinstance(plan, dict):
        raise ValueError("the plan is receivers, how often and where")
    out = {}
    for base, how in plan.items():
        if not isinstance(how, dict) or isinstance(how.get("every_h"), bool) or how.get("every_h") not in EVERY_H:
            raise ValueError("a receiver is surveyed every 1, 2, 3 or 6 hours")
        out[clean_base(base)] = {"every_h": how["every_h"], "places": clean_places(how.get("places"))}
    return out


class Rounds:
    """Surveys the receivers in its plan by itself (see the top): on the hour, every so many hours, while the radio
    isn't playing that receiver. playing() is the address of what the radio is playing now; tick() is called a
    couple of times a minute."""

    def __init__(self, job: Job, playing: Callable[[], str | None] = lambda: None, clock: Callable[[], float] = time.time,
                 localtime: Callable[[float], time.struct_time] = time.localtime) -> None:
        self.job, self.playing = job, playing
        self._clock, self._localtime = clock, localtime
        self.plan: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._did: dict[str, str] = {}               # receiver -> the hour ("2026-10-12 14") it was last begun in

    def set_plan(self, plan: dict[str, dict]) -> None:
        with self._lock:
            self.plan = {base: dict(how) for base, how in plan.items()}
            for base in [b for b in self._did if b not in plan]:
                del self._did[base]

    def status(self) -> dict:
        """{"plan": {receiver: {"every_h", "places": how many}}}"""
        with self._lock:
            return {"plan": {base: {"every_h": how["every_h"], "places": len(how["places"])} for base, how in self.plan.items()}}

    def tick(self) -> None:
        if self.job.status().get("running") or calibrate.job.status().get("running"):
            return
        now = self._clock()
        t = self._localtime(now)
        hour = time.strftime("%Y-%m-%d %H", t)
        on = self.playing() or ""
        with self._lock:
            plan = {base: dict(how) for base, how in self.plan.items()}
        for base, how in plan.items():
            if t.tm_hour % how["every_h"] or self._did.get(base) == hour or on.startswith(base):
                continue
            try:
                self.job.start(base, how["places"], "itself")
            except ValueError:
                return
            self._did[base] = hour
            return

    def start(self) -> None:
        threading.Thread(target=self._run, name="survey-rounds", daemon=True).start()

    def _run(self) -> None:
        while True:
            time.sleep(TICK_S)
            try:
                self.tick()
            except Exception:                        # (one that falls over mustn't end the rounds)
                log.exception("survey: by itself failed")


job = Job()
rounds = Rounds(job)
