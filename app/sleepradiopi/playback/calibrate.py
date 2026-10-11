"""A receiver of your own, put on frequency: the dongle's error measured off the air and set.

A dongle behind an upconverter is two oscillators, neither exact: the dongle
is out by so many parts per million (so by more the higher it's tuned), and
the upconverter by a fixed amount. On short wave that is a few hundred Hz
that changes from band to band, and drifts as the dongle warms: nothing to
FT8, which doesn't care, but a voice in sideband sounds wrong 50 Hz out.

Short wave is full of stations on known frequencies:

  * Broadcast stations keep to a 5 kHz raster, to a few Hz. Tuned 1 kHz
    under one in upper sideband its carrier is a 1000 Hz tone; what it is
    instead is the error there. Which are on the air changes by the hour, so
    they're found each time: the strongest carriers in the receiver's own
    waterfall of each broadcast band.
  * The German weather service's teleprinters (audio/rtty.py finds the two
    tones): tuned 1.5 kHz under, their middle is 1500 Hz.

The errors lie on a line against frequency. Its slope is what the dongle's
parts-per-million setting is out by; what's left when that's put right is
the trim. Both are given to the receiver (its POST /calibrate), which keeps
them. With too few stations heard, or all in one band, only the trim is
moved -- or nothing, and it says so. A station that doesn't fit the line
(a carrier that wasn't a broadcast station) is left out.

The receiver hears one thing at a time, so it's taken round for a minute
and a half and then put back; if the radio is playing it, that's heard.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from base64 import b64decode
from typing import Callable

import numpy as np

from sleepradiopi.audio import rtty
from sleepradiopi.audio.psk import spectrum

log = logging.getLogger(__name__)

AUDIO_RATE = 12_000          # what a receiver sends when it's tuned like a rig
RASTER_HZ = 5_000            # broadcast stations are on multiples of this
# the short-wave broadcast bands, Hz: where carriers are looked for
BROADCAST = ((5_900_000, 6_200_000), (7_200_000, 7_450_000), (9_400_000, 9_900_000), (11_600_000, 12_100_000),
             (13_570_000, 13_870_000), (15_100_000, 15_800_000), (17_480_000, 17_900_000))
WEATHER_HZ = (7_646_000, 10_100_800, 11_039_000)    # the German weather service's teleprinters
CARRIER_OVER_DB = 15.0       # a carrier in the waterfall: this far over the band's noise
PER_BAND = 2                 # carriers measured in each band
TONE_OVER_DB = 20.0          # the carrier as a tone in the sound: this far over the rest
TONE_REACH_HZ = 700.0        # ...and no further than this from where it should be (so the error can be this much)
SETTLE_S, LISTEN_S, WATERFALL_S = 0.9, 1.6, 2.2
OUTLIER_HZ = 60.0            # a station this far off the line through the others is left out
LINE_FROM, LINE_SPAN_HZ = 4, 4_000_000      # stations, and how far apart the furthest must be, for the slope to be believed
PPM_MOST = 200


def carriers(rows: list[bytes], centre: int, rate: int, per_db: float, lo: int, hi: int, most: int = PER_BAND) -> list[int]:
    """The strongest broadcast carriers in a receiver's waterfall rows (a byte a bin, the lowest frequency first)
    -> their raster frequencies, Hz, the strongest first: between lo and hi, not at the dongle's own centre."""
    if not rows:
        return []
    n = min(len(r) for r in rows)
    p = np.mean([np.frombuffer(r[:n], np.uint8).astype(float) for r in rows], axis=0) / per_db       # dB, from wherever
    f = centre + (np.arange(n) - n / 2) * rate / n
    floor = float(np.median(p))
    found: list[int] = []
    for i in np.argsort(p)[::-1]:
        if p[i] - floor < CARRIER_OVER_DB or len(found) >= most:
            break
        if not lo <= f[i] <= hi or abs(f[i] - centre) < 3_000:
            continue
        if not (p[i] >= p[max(0, i - 2):i + 3]).all():           # (the top of its hump)
            continue
        hz = int(round(f[i] / RASTER_HZ)) * RASTER_HZ
        if all(abs(hz - g) >= 2 * RASTER_HZ for g in found):
            found.append(hz)
    return found


def tone(x: np.ndarray, rate: float = AUDIO_RATE, near: float = 1000.0) -> float | None:
    """The pitch of a steady carrier in x within TONE_REACH_HZ of `near`, to a Hz or so; None if there isn't one
    standing clear."""
    if len(x) < rate * 0.5:
        return None
    w = np.hanning(len(x))
    n = 1 << int(np.ceil(np.log2(len(x) * 4)))       # (padded: a finer look at the top)
    p = np.abs(np.fft.rfft(x * w, n)) ** 2
    f = np.fft.rfftfreq(n, 1 / rate)
    band = (f >= 300) & (f <= 2700)
    look = np.flatnonzero((f >= near - TONE_REACH_HZ) & (f <= near + TONE_REACH_HZ) & band)
    if not len(look) or not band.any():
        return None
    i = look[np.argmax(p[look])]
    if p[i] < 0.5 * p[band].max():                   # (the carrier is the strongest thing there: not a note of the programme)
        return None
    if 10 * np.log10(p[i] / (np.median(p[band]) + 1e-30)) < TONE_OVER_DB + 10 * np.log10(len(x) / rate):   # (a tone grows with the length looked at)
        return None
    a, b, c = np.log(p[i - 1:i + 2] + 1e-30)         # the top between the bins
    return float(f[i] + 0.5 * (a - c) / (a - 2 * b + c) * f[1])


def weather(x: np.ndarray, rate: float = AUDIO_RATE) -> float | None:
    """The middle of a weather teleprinter's two tones in x (1500 Hz when on frequency), or None."""
    found = rtty.find(x, rate)
    if found is None or found[1] != 450.0:
        return None
    f, p = spectrum(x, rate, 8192)                   # each tone's own middle, finer than the finding

    def middle(about: float) -> float:
        m = (f > about - 60) & (f < about + 60)
        return float(np.sum(f[m] * p[m]) / np.sum(p[m]))
    return (middle(found[0]) + middle(found[0] + 450.0)) / 2


def fit(points: list[tuple[int, float]], ppm: int, trim: int, shift_hz: int) -> dict:
    """What the measurements ask for. points: [(the station's frequency Hz, how far above where it should be its
    tone was, Hz)]; ppm, trim, shift_hz: the receiver's settings they were taken with (shift: its upconverter).
    -> {"ppm", "trim" (to set), "slope" (ppm found, None if only the trim could be told), "used": [(Hz, error)],
    "left_out": [...], "was" (the worst error among those used), "left" (the worst that should remain)}.
    ValueError says why nothing can be told."""
    used, out = sorted(points), []
    while True:
        if len(used) < 2:
            raise ValueError("too few stations of known frequency were heard to go by: try in the evening, when the broadcast bands are full")
        f, e = np.array([p[0] for p in used], float), np.array([p[1] for p in used], float)
        line = len(used) >= LINE_FROM and f.max() - f.min() >= LINE_SPAN_HZ
        slope, level = np.polyfit(f, e, 1) if line else (0.0, float(np.median(e)))
        off = np.abs(e - (slope * f + level))
        worst = int(np.argmax(off))
        if off[worst] <= OUTLIER_HZ:
            break
        out.append(used.pop(worst))
    step = -int(round(slope * 1e6)) if line else 0   # (the setting is in whole parts per million)
    if abs(ppm + step) > PPM_MOST:
        raise ValueError("the stations heard ask for more correction than a dongle can need: nothing was changed")
    after = e + step * 1e-6 * (shift_hz + f)         # what each would read with that set...
    move = int(round(float(np.mean(after))))         # ...and what's left is the trim's
    return {"ppm": ppm + step, "trim": trim + move, "slope": round(float(slope * 1e6), 2) if line else None,
            "used": [(int(a), round(float(b), 1)) for a, b in zip(f, e)], "left_out": [(int(a), round(float(b), 1)) for a, b in out],
            "was": round(float(np.abs(e).max()), 1), "left": round(float(np.abs(after - move).max()), 1)}


def _get(url: str, timeout: float = 8.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read()).get("error")
        except ValueError:
            said = None
        raise ValueError(said or f"the receiver said {e.code}") from None


class _Sound:
    """A receiver's audio stream, read as it comes: take(seconds) is the next that many, from now."""

    def __init__(self, url: str) -> None:
        self._r = urllib.request.urlopen(url, timeout=10)
        self._r.read(44)                             # (the WAV header)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._closed = False
        threading.Thread(target=self._run, name="calibrate-sound", daemon=True).start()

    def _run(self) -> None:
        try:
            while not self._closed:
                b = self._r.read(2400)
                if not b:
                    break
                with self._lock:
                    self._buf += b
                    del self._buf[:-AUDIO_RATE * 2 * 20]
        except (OSError, ValueError):
            pass
        self._closed = True

    def take(self, seconds: float, settle: float = SETTLE_S) -> np.ndarray:
        time.sleep(settle)
        with self._lock:
            self._buf.clear()
        until = time.monotonic() + seconds + 6
        while time.monotonic() < until and not self._closed:
            with self._lock:
                if len(self._buf) >= int(seconds * AUDIO_RATE) * 2:
                    return np.frombuffer(bytes(self._buf[:int(seconds * AUDIO_RATE) * 2]), "<i2").astype(np.float32)
            time.sleep(0.05)
        raise ValueError("the receiver's sound stopped coming")

    def close(self) -> None:
        self._closed = True
        try:
            self._r.close()
        except OSError:
            pass


class Job:
    """One calibration of one receiver, in a thread of its own: start(base), then status() until it's done."""

    def __init__(self, get: Callable = _get, post: Callable = _post, sound: Callable = _Sound) -> None:
        self._get, self._post, self._sound = get, post, sound
        self._lock = threading.Lock()
        self._state: dict = {"running": False}
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        """{"running", "base", "step" (what it's doing), "points": [[Hz, error]], then "result": fit()'s with "now"
        (the worst error on checking), or "error"}"""
        with self._lock:
            return json.loads(json.dumps(self._state))

    def start(self, base: str) -> None:
        """ValueError if one is running."""
        with self._lock:
            if self._state.get("running"):
                raise ValueError("a calibration is already running")
            self._state = {"running": True, "base": base, "step": "Asking the receiver…", "points": []}
            self._thread = threading.Thread(target=self._run, args=(base,), name="calibrate", daemon=True)
            self._thread.start()

    def set(self, base: str, ppm: int, trim: int) -> dict:
        """Put a receiver's ppm and trim to these (to undo a calibration)."""
        st = self._post(base + "/calibrate", {"ppm": ppm, "trim": trim})
        return {"ppm": st.get("ppm"), "trim": st.get("trim")}

    def _say(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _run(self, base: str) -> None:
        sound = None
        try:
            st = self._get(base + "/status")
            shift = int(st.get("upconverter") or 0)
            if not shift or "ppm" not in st:
                raise ValueError("this is for a short-wave receiver of your own with the newer receiver software" if shift else
                                 "this is for a short-wave receiver (one behind an upconverter): the stations it goes by are on short wave")
            ppm, trim, before = int(st["ppm"]), int(st["trim"]), st.get("receiving") if st.get("tunes") else None
            lo, hi = st.get("tunable") or (0, 30_000_000)
            # (someone listening and tuning it: joined as it is, then moved; nobody: started)
            sound = self._sound(base + ("/audio" if before else "/audio?freq=7.3&mode=usb"))
            go = lambda hz: self._post(base + "/tune", {"freq": int(hz), "mode": "usb"})
            points: list[tuple[int, float]] = []

            def measure(hz: int) -> float | None:
                go(hz - 1000)
                return (lambda t: None if t is None else t - 1000.0)(tone(sound.take(LISTEN_S)))

            bands = [b for b in BROADCAST if b[0] >= lo and b[1] <= hi]
            for k, (b0, b1) in enumerate(bands):
                self._say(step=f"Looking for broadcast stations: {b0 / 1e6:.1f} to {b1 / 1e6:.1f} MHz ({k + 1} of {len(bands)})…")
                found: list[int] = []
                for centre in ([(b0 + b1) // 2] if b1 - b0 <= 260_000 else [b0 + (b1 - b0) // 4, b1 - (b1 - b0) // 4]):
                    go(centre)
                    since = self._get(base + "/spectrum?since=0").get("n", 0)       # (asking is what starts them being kept)
                    time.sleep(WATERFALL_S)
                    w = self._get(base + f"/spectrum?since={since}")
                    rows = [b64decode(r[1]) for r in w.get("rows", [])]
                    found += [hz for hz in carriers(rows, int(w.get("centre", centre)), int(w.get("rate", 256_000)), float(w.get("per_db", 2.5)), b0, b1) if hz not in found]
                for hz in found[:PER_BAND + 1]:
                    err = measure(hz)
                    if err is not None:
                        points.append((hz, round(err, 1)))
                        self._say(points=[list(p) for p in points])
            for hz in (w for w in WEATHER_HZ if lo <= w <= hi):
                self._say(step=f"Listening for the weather teleprinter on {hz / 1e6:.4f} MHz…")
                go(hz - 1500)
                mid = weather(sound.take(3.0))
                if mid is not None:
                    points.append((hz, round(mid - 1500.0, 1)))
                    self._say(points=[list(p) for p in points])
            self._say(step="Working it out…")
            result = fit(points, ppm, trim, shift)
            if (result["ppm"], result["trim"]) != (ppm, trim):
                self._say(step="Setting the receiver…")
                self._post(base + "/calibrate", {"ppm": result["ppm"], "trim": result["trim"]})
                time.sleep(2.5)                      # (it starts afresh)
                check = [p[0] for p in result["used"]]
                check = [check[i] for i in sorted({0, len(check) // 2, len(check) - 1})]
                now = [e for e in (measure(hz) for hz in check) if e is not None]
                result["now"] = round(max(abs(e) for e in now), 1) if now else None
            else:
                result["now"] = result["was"]
            result.update(before={"ppm": ppm, "trim": trim})
            if before:                               # back where it was
                try:
                    self._post(base + "/tune", {"freq": before["freq"], "mode": before["mode"]})
                except ValueError:
                    pass
            self._say(running=False, step="Done.", result=result)
        except (ValueError, OSError, KeyError, TypeError) as e:
            log.warning("calibrate: %s", e)
            self._say(running=False, step="", error=str(e) if isinstance(e, ValueError) else f"the receiver isn't answering ({e})")
        finally:
            if sound is not None:
                sound.close()


job = Job()
