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

The finder: amateur teleprinters and Morse have no one frequency -- they
are wherever in their part of the band somebody is sending -- so a place
of mode "rtty" or "cw" is a stretch of band ("lo" to "hi"), swept:

  * The receiver's own waterfall (its /spectrum: a quarter of a megahertz
    at once, 250 Hz a bin) is watched for a few seconds with the receiver
    in the middle of the stretch. What matters is what comes and goes: a
    dongle has steady whistles of its own every 30 kHz, as strong as any
    station, and they never move (active()). Morse is keyed, so it swings
    by 10 or 20 dB; a teleprinter is steady while it sends but not dead
    steady.
  * Each candidate, the strongest first, is then listened to for a few
    seconds, tuned so that it falls in the middle of what's heard, and
    the reader for the mode is the judge. Morse: the Morse reader
    (audio/morse.py) reads those seconds, and it is Morse if what it
    read holds together (its "quality") at a speed people send at. A
    teleprinter: a pair of tones that frames, one tone at a time
    (audio/rtty.py). FT4 and the rest, which also come and go, are
    neither and are left out.

What's found is where each is ("where": the frequency on the air, with a
Morse signal's speed or a teleprinter's speed and shift), the strongest
first. Worked out on 20, 30 and 40 m off the air, where simpler tests
failed: a tone that merely comes and goes (and even a count of its marks
and their lengths) took a carrier with noise on it for Morse, and the
teleprinter reader's framing alone took two steady tones on 14.090 MHz
for a teleprinter. Against what the Morse reader made of a quarter of a
minute of each: quality 0.57 to 0.99 in eight seconds for real contacts
("UR SIGNA...", "CQ CQ DE EA1..."), 0.02 to 0.17 for the rest.

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

from base64 import b64decode

from sleepradiopi.audio import ft8, morse, psk, rtty
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback import calibrate

log = logging.getLogger(__name__)

AUDIO_RATE = calibrate.AUDIO_RATE
MODES = ("ft8", "ft4", "wspr", "psk31", "weather", "rtty", "cw")
FIND_MODES = ("rtty", "cw")          # ...of which these are a stretch of band, swept (the finder)
SPAN_MOST_HZ = 150_000               # a stretch is no wider than this (the waterfall shows 256 kHz)
WATERFALL_S = 5.0                    # the waterfall watched this long
OVER_DB = 7.0                        # something there: this far over the stretch's noise at its best
SWING_DB = {"cw": 5.0, "rtty": 1.0}  # ...and it comes and goes by this much (a dongle's own whistles: under half a dB)
CENTRE_CLEAR_HZ = 1_500              # (the dongle's own centre is a spike)
FIND_MOST = 6                        # candidates listened to, a stretch
FIND_LISTEN_S = {"cw": 8.0, "rtty": 4.5}     # how long each candidate is listened to (five seconds was too few for the Morse reader to be sure)
MORSE_QUALITY_MIN = 0.3              # what the Morse reader read holds together this well, or it isn't Morse...
MORSE_WPM = (8.0, 45.0)              # ...at a speed people send at
PITCH_HZ = {"cw": 700.0, "rtty": 1300.0}     # where a candidate is put in what's heard
SAME_HZ = {"cw": 150.0, "rtty": 300.0}       # two findings this close are one signal
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
        place = {"key": key, "mode": p["mode"], "band": band, "hz": int(hz)}
        if p["mode"] in FIND_MODES:
            lo, hi = p.get("lo"), p.get("hi")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, hi)) or not 100_000 <= lo < hi <= 500_000_000 or hi - lo > SPAN_MOST_HZ:
                raise ValueError("a place to sweep has where it begins and ends (lo and hi, in Hz), no more than 150 kHz apart")
            place.update(lo=int(lo), hi=int(hi))
        out.append(place)
    return out


def active(rows: np.ndarray, centre: float, rate: float, lo: float, hi: float, swing_db: float, most: int = FIND_MOST) -> list[tuple[int, float, float]]:
    """What comes and goes between lo and hi in a waterfall's rows (dB, a row a moment, the lowest frequency
    first) -> [(Hz, dB over the noise at its best, how much it comes and goes, dB)], the strongest first."""
    if len(rows) < 8:
        return []
    n = rows.shape[1]
    f = centre + (np.arange(n) - n / 2) * rate / n
    top, low = np.percentile(rows, 90, axis=0), np.percentile(rows, 10, axis=0)
    inside = (f >= lo) & (f <= hi) & (np.abs(f - centre) > CENTRE_CLEAR_HZ)
    if not inside.any():
        return []
    floor = float(np.median(np.median(rows, axis=0)[inside]))
    found = []
    for i in np.argsort(top)[::-1]:
        if top[i] - floor < OVER_DB or len(found) >= most:
            break
        if not inside[i] or not (top[i] >= top[max(0, i - 1):i + 2]).all() or top[i] - low[i] < swing_db:
            continue
        found.append((int(f[i]), round(float(top[i] - floor), 1), round(float(top[i] - low[i]), 1)))
    return found


def heard_as(x: np.ndarray, mode: str) -> list[dict]:
    """What a few seconds of sound at 12 kHz hold of the mode looked for -> [{"hz" (in the sound), "wpm" (Morse),
    "baud", "shift" (a teleprinter)}]: the strongest keyed tone, if the Morse reader makes Morse of it, or the
    pairs of tones that frame as a teleprinter."""
    if len(x) < 2 * AUDIO_RATE:
        return []
    if mode == "cw":
        pitch = morse.strongest_tone(x, AUDIO_RATE)
        if pitch is None:
            return []
        read = morse.decode(x, AUDIO_RATE, pitch, morse.noise_level(x, AUDIO_RATE, pitch)[2])
        if read["quality"] < MORSE_QUALITY_MIN or not MORSE_WPM[0] <= read["wpm"] <= MORSE_WPM[1]:
            return []
        return [{"hz": read["pitch"], "wpm": round(read["wpm"])}]
    out = []
    for low, shift, _ in rtty.find_all(x, AUDIO_RATE, most=2):
        r = rtty.read(x, AUDIO_RATE, low, shift)
        if r["codes"]:
            out.append({"hz": low + shift / 2, "baud": r["baud"], "shift": round(shift)})
    return out


def clean_base(base) -> str:
    base = str(base).rstrip("/")
    if not base.startswith(("http://", "https://")) or any(c.isspace() for c in base):
        raise ValueError("base is the receiver's address: http://192.168.50.154:8074")
    return base


def slot_heard(x: np.ndarray, began: float, t0: float, mode: str, decode: Callable) -> dict:
    """The slot that began at t0 by the clock, in sound at 12 kHz that began at `began` -> {"n": stations heard,
    "km"?: the farthest, "to"?: how many in each part of the world ({"EU": 9, "NA": 3})}: cut each of a few ways late, the way that reads most."""
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
    far, grids = 0, []
    for m in best:
        call, grid = (m.get("call"), m.get("grid")) if mode == "wspr" else ft8.who(m["text"])[:2]
        if call and grid:
            grids.append(grid)
            if ft8.HOME:
                far = max(far, ft8.path(ft8.HOME, grid)[0])
    from sleepradiopi.playback import conditions     # (which parts of the world they're in: what the band is open to)
    to = conditions.regions(grids)
    return {"n": len(best), **({"km": far} if far else {}), **({"to": to} if to else {})}


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
                                            **({"where": [dict(a) for a in f["where"]]} if isinstance(f.get("where"), list) else {}),
                                            **({"to": {str(r): int(n) for r, n in f["to"].items()}} if isinstance(f.get("to"), dict) else {}),
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
        finding of the last day, the oldest first), "where"? (a stretch swept: where each signal found is, [{"hz", "wpm"? |
        "baud", "shift"}], the strongest first)}}"""
        now = self._clock()
        with self._lock:
            return {key: {**f, "day": [d for d in f["day"] if now - d[0] <= DAY_S]} for key, f in self._found.get(base, {}).items()}

    def bases(self) -> list[str]:
        """The receivers something has been found on."""
        with self._lock:
            return list(self._found)

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

    def _find(self, place: dict, base: str, sound, stop: threading.Event) -> dict | None:
        """A stretch of band swept for Morse or teleprinters (see the top). None: stopped."""
        mode, lo, hi = place["mode"], place["lo"], place["hi"]
        self._post(base + "/tune", {"freq": (lo + hi) // 2, "mode": "usb"})
        if not self._wait(SETTLE_S, stop):
            return None
        since = self._get(base + "/spectrum?since=0").get("n", 0)       # (asking is what starts them being kept)
        if not self._wait(WATERFALL_S, stop):
            return None
        w = self._get(base + f"/spectrum?since={since}")
        rows = [np.frombuffer(b64decode(r[1]), np.uint8) for r in w.get("rows", [])]
        if not rows:
            raise ValueError("this receiver has no waterfall to look for them in")
        width = min(len(r) for r in rows)
        rows = np.array([r[:width] for r in rows], float) / float(w.get("per_db", 2.5))
        signals: list[dict] = []
        for hz, db, _ in active(rows, float(w.get("centre", (lo + hi) // 2)), float(w.get("rate", 256_000)), lo, hi, SWING_DB[mode]):
            dial = int(hz - PITCH_HZ[mode])
            self._post(base + "/tune", {"freq": dial, "mode": "usb"})
            if not self._wait(SETTLE_S, stop):
                return None
            sound.mark()
            if not self._wait(FIND_LISTEN_S[mode], stop):
                return None
            for s in heard_as(sound.since_mark(), mode):
                on_air = int(round(dial + s["hz"]))
                if lo - 1_000 <= on_air <= hi + 1_000 and all(abs(on_air - g["hz"]) > SAME_HZ[mode] for g in signals):
                    signals.append({**s, "hz": on_air, "db": db})
        return {"n": len(signals), "where": [{k: v for k, v in s.items() if k != "db"} for s in sorted(signals, key=lambda s: -s["db"])]}

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
                if place["mode"] in FIND_MODES:
                    try:
                        heard = self._find(place, base, sound, stop)
                    except ValueError as e:          # (no waterfall to look in: that place is left, the round goes on)
                        log.info("survey: %s: %s", place["key"], e)
                        continue
                else:
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
        except Exception:                            # (anything else: said, and the round over -- never left "running" for good)
            log.exception("survey: the round fell over")
            with self._lock:
                self._state.update(running=False, at=None, done=done, error="it stopped unexpectedly (the radio's log says why)")
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
