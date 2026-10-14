"""FT8 and FT4 read off a receiver's audio, as the messages sent.

FT8 and FT4 are the amateur bands' weak-signal modes: every station sends a
short fixed message (a call, an answer, a report) in a slot of its own, FT8's
15 seconds long and FT4's 7.5, all of them starting on the clock. A receiver
left on a band's FT8 frequency in upper sideband hears some dozens of them
at once, each a warble about 50 Hz wide, and a slot's worth of sound reads
back as a list: who called whom, how strong, from where.

How it's read:

  * The sound a receiver plays is kept, with the time by the clock at which
    it came (broadcast/station.py feeds it, as it does the Morse reader).
  * When a slot has ended its sound is cut out, brought to the 12 kHz the
    decoder wants, and given to it as a WAV file.
  * The decoding is ft8_lib's (github.com/kgoba/ft8_lib, MIT): its
    decode_ft8 program, in the image as /usr/bin/decode_ft8. It runs as a
    process of its own at low priority, so it never takes the processor from
    the thread playing the sound; a slot takes it a fraction of a second.

The clock matters and the sound is late, by how much depending on how it
comes: a receiver the radio tunes itself is half a second behind, a stream
played through the radio's buffer three or four (on the radio a receiver
of the owner's own was 3.6 s late, and a cut a second on read nothing of a
band full of signals). The decoder reads as much with the cut anywhere
from a second and a half early to two seconds late (measured on 20 m), so:

  * Until something is read, a slot is cut several ways -- 1, 3, 5 and 7
    seconds late -- and the cut that reads most is the one.
  * Each message says how far into its slot's sound it began (half a
    second, when the cut is right). From then on the cut follows that, and
    one decoding a slot is enough.
  * A few slots with nothing read and it looks all ways again: the stream
    may have begun again with another delay.

A radio whose clock is seconds out is only another delay to this, within
those seven seconds; the time comes from the network (and the clock chip
between times).

Who and how far: most messages name the station sending them, and a first
call gives its locator (JO31: a square of 1 degree by 2, about 70 miles by
70 here). With the radio's own locator known (HOME: Settings keep it), each
is given a distance and a bearing from the middle of its square, and the
station goes on the list of who has been heard on each band, the one every
reader hands its stations to (audio/heard.py: when first and last, how
strong at best, how often). A station's later messages carry no locator;
they get the one it gave before. The list is kept on the card (keep_in()
says where), sparingly: a quarter of an hour apart at most, when it has
changed, and when the reader is switched off; so a restart loses a few
minutes of it at worst.

A message that names the radio's owner (the callsign kept for the rooms:
playback/room.py) is marked as theirs: somebody calling or answering them.

WSPR is read the same way, by the same reader: beacons of a watt or less,
each sending its call, locator and power over two minutes, starting on the
even minute, in a 200 Hz slice of each band. Its decoder is wsprd (K9AN and
K1JT's, from WSJT-X: GPL; the last version that is all C, in the image as
/usr/bin/wsprd), which takes anything from four seconds early to four late
in its stride, so a slot is simply cut two seconds on. A slot takes it one
to seven seconds on the Zero 2 W.

Receive only: nothing is sent on the air. What's read can be reported to
PSK Reporter and WSPRnet (audio/report.py: off until the owner switches it
on), but only from a receiver of the owner's own: the page says the
receiver is one (and where it's tuned) with the band, and reports() checks
the radio is still playing it.

`reader` is the one the radio uses: the station feeds it, the web side
switches it and asks what it has read.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from sleepradiopi.audio import heard as heard_mod, pcm

log = logging.getLogger(__name__)

MODES = {"ft8": 15.0, "ft4": 7.5, "wspr": 120.0}      # a slot, in seconds
USED_S = {"wspr": 114.0}             # ...and how much of one the decoder is given, where that's less
WSPR_LATE_S = 2.0                    # WSPR: a slot is cut this late, and that's that (see the top)
OUT_RATE = 12_000                    # what the decoder takes
DECIMATE = 3                         # of the speaker's rate: 14700 samples a second holds a receiver's 3 kHz with room to spare
RATE = pcm.SAMPLE_RATE / DECIMATE
SEARCH_LATE_S = (1.0, 3.0, 5.0, 7.0)     # how late a slot is cut, each tried, until something is read (see the top)
ON_TIME_DT = 0.5             # how far into its slot's sound a message begins when the cut is right
LATE_MAX_S = 9.0
LOST_AFTER = 4               # slots with nothing read before every cut is tried again
WAIT_S = 0.3                 # ...and looked at this long after it has all come
GAP_S = 1.0                  # sound this far from when it was due: the receiver stopped and started
FOLLOW = 0.05                # (how fast the clock is followed, a block)
ENOUGH = 0.9                 # of a slot's sound, for it to be worth reading
KEEP_SLOTS, KEEP_MORE_S = 2, 10.0    # the sound kept: this many of the mode's slots and a bit
STEP_S = 0.25
DECODE_TIMEOUT_S = 12.0      # (a slot's reading that takes longer than this is given up)
WSPR_TIMEOUT_S = 100.0
DECODER = "decode_ft8"       # ft8_lib's; SLEEPRADIO_FT8_DECODER names another
WSPR_DECODER = "wsprd"       # WSJT-X's; SLEEPRADIO_WSPR_DECODER names another
WSPR_LINE = re.compile(r"^\S+\s+(-?\d+)\s+(-?\d+\.\d+)\s+(\d+\.\d+)\s+(-?\d+)\s+(\S.*?)\s*$")
WSPR_GRID = re.compile(r"^[A-R]{2}\d{2}(?:[A-Xa-x]{2})?$")
HOME: str | None = None      # the radio's own locator ("IO94" or "IO94kn"): main.py sets it from the settings
GRID = re.compile(r"^[A-R]{2}\d{2}$")
CALL = re.compile(r"^(?:[A-Z0-9]{1,4}/)?(?:[A-Z]{1,2}\d|\d[A-Z])\d{0,2}[A-Z]{1,4}(?:/[A-Z0-9]{1,4})?$")
LINE = re.compile(r"^\d{6}\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+(\d+)\s+~\s+(\S.*?)\s*$")


def decoder_path() -> str | None:
    """The decoding program, or None: this radio can't read these."""
    named = os.environ.get("SLEEPRADIO_FT8_DECODER")
    if named:
        return named if os.access(named, os.X_OK) else None
    return shutil.which(DECODER)


def wspr_decoder_path() -> str | None:
    """wsprd, or None: this radio can't read WSPR."""
    named = os.environ.get("SLEEPRADIO_WSPR_DECODER")
    if named:
        return named if os.access(named, os.X_OK) else None
    return shutil.which(WSPR_DECODER)


def can(mode: str) -> bool:
    """Is there a decoder here for this mode?"""
    return (wspr_decoder_path() if mode == "wspr" else decoder_path()) is not None


def _taps(rate: float, cutoff: float, n: int = 63) -> np.ndarray:
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / rate * k) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


_LOW_PASS = _taps(RATE, 4500.0)


def to_decoder_rate(x: np.ndarray) -> np.ndarray:
    """Sound at RATE -> at OUT_RATE, as 16-bit samples."""
    if not len(x):
        return np.zeros(0, np.int16)
    y = np.convolve(x, _LOW_PASS, mode="same")
    n = int(len(x) * OUT_RATE / RATE)
    y = np.interp(np.arange(n) * (RATE / OUT_RATE), np.arange(len(y)), y)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


def parse(out: str) -> list[dict]:
    """What decode_ft8 prints -> [{"snr" (dB), "dt" (s into the slot), "hz" (in the sound), "text"}], strongest first."""
    found = []
    for line in out.splitlines():
        m = LINE.match(line)
        if m:
            found.append({"snr": float(m.group(1)), "dt": float(m.group(2)), "hz": int(m.group(3)), "text": " ".join(m.group(4).split())})
    return sorted(found, key=lambda d: -d["snr"])


def clean_locator(text) -> str:
    """A locator as typed -> "IO94" or "IO94kn" ("" for none). ValueError says what one looks like."""
    t = "".join(str(text or "").split())
    if not t:
        return ""
    if not re.fullmatch(r"[A-Ra-r]{2}\d{2}(?:[A-Xa-x]{2})?", t):
        raise ValueError("a locator is two letters and two figures, with two more letters if you like: IO94 or IO94kn")
    return t[:4].upper() + t[4:].lower()


def locate(locator: str) -> tuple[float, float]:
    """The middle of a locator's square -> (latitude, longitude), degrees north and east."""
    t = locator.upper()
    lon = (ord(t[0]) - 65) * 20 - 180 + int(t[2]) * 2
    lat = (ord(t[1]) - 65) * 10 - 90 + int(t[3])
    if len(t) >= 6:
        return lat + (ord(t[5]) - 65 + 0.5) / 24, lon + (ord(t[4]) - 65 + 0.5) / 12
    return lat + 0.5, lon + 1.0


def path(home: str, there: str) -> tuple[int, int]:
    """From one locator to another, the short way round -> (km, the bearing to set off on: degrees from north)."""
    (lat1, lon1), (lat2, lon2) = (tuple(math.radians(v) for v in locate(x)) for x in (home, there))
    d = lon2 - lon1
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d / 2) ** 2
    km = 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))
    deg = math.degrees(math.atan2(math.sin(d) * math.cos(lat2), math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d)))
    return round(km), round(deg) % 360


def who(text: str) -> tuple[str | None, str | None, bool]:
    """A message -> (the station sending it, the locator it gives, whether it's a CQ). "CQ DX K1ABC FN42",
    "K1ABC W9XYZ EN37", "K1ABC W9XYZ R-11": the sender is the second call, or the one after CQ and whatever
    it's calling for. A call the decoder only has a hash of (<...>) is nobody."""
    words = text.split()
    cq = bool(words) and words[0] == "CQ"
    rest = [w.strip("<>") for w in (words[1:] if words else [])]
    if cq and len(rest) > 1 and not CALL.match(rest[0]):         # (CQ DX, CQ POTA, CQ 123: what it's calling for)
        rest = rest[1:]
    call = rest[0] if rest and CALL.match(rest[0]) and not GRID.match(rest[0]) else None
    grid = rest[-1] if len(rest) > 1 and GRID.match(rest[-1]) and rest[-1] != "RR73" else None
    return call, grid if call else None, cq


def parse_wspr(out: str) -> list[dict]:
    """What wsprd prints -> [{"snr", "dt", "hz" (in the sound), "drift" (Hz a minute), "text", "call"?, "grid"?,
    "dbm"? (the power it says it's sending)}], strongest first. A spot is "K1ABC FN42 37"; a call with a prefix
    comes without its locator ("PJ4/K1ABC 37") and then again as "<PJ4/K1ABC> FK52ud 37"."""
    found = []
    for line in out.splitlines():
        m = WSPR_LINE.match(line)
        if not m or "DecodeFinished" in line:
            continue
        text = " ".join(m.group(5).split())
        words = text.split()
        spot = {"snr": float(m.group(1)), "dt": float(m.group(2)), "hz": int(round(float(m.group(3)) * 1e6)),      # (no dial given: MHz of the sound)
                "drift": int(m.group(4)), "text": text}
        call = words[0].strip("<>") if words else ""
        if call and call != "...":
            spot["call"] = call
            if len(words) == 3 and WSPR_GRID.match(words[1]):
                spot["grid"] = words[1][:4].upper() + words[1][4:].lower()
        if words and re.fullmatch(r"-?\d+", words[-1]):
            spot["dbm"] = int(words[-1])
        found.append(spot)
    return sorted(found, key=lambda d: -d["snr"])


def _run_wspr(samples: np.ndarray) -> list[dict]:
    program = wspr_decoder_path()
    if program is None:
        raise RuntimeError("no WSPR decoder on this radio")
    kept = os.path.join(tempfile.gettempdir(), "sleepradio-wspr")      # (it keeps the calls it has learnt here, and its FFT plans)
    os.makedirs(kept, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wspr-") as tmp:
        path = os.path.join(tmp, "slot.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(samples.astype("<i2").tobytes())
        cmd = [program, "-a", kept, path]
        nice = shutil.which("nice")
        try:
            done = subprocess.run([nice, "-n", "10", *cmd] if nice else cmd, capture_output=True, text=True, timeout=WSPR_TIMEOUT_S, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise RuntimeError("the decoder took too long") from None
        except OSError as e:
            raise RuntimeError(f"the decoder wouldn't start: {e}") from None
    return parse_wspr(done.stdout)


def run_decoder(samples: np.ndarray, mode: str) -> list[dict]:
    """A slot of sound at OUT_RATE -> the messages in it. RuntimeError says why not."""
    if mode == "wspr":
        return _run_wspr(samples)
    program = decoder_path()
    if program is None:
        raise RuntimeError("no decoder on this radio")
    with tempfile.TemporaryDirectory(prefix="ft8-") as tmp:
        path = os.path.join(tmp, "slot.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(samples.astype("<i2").tobytes())
        cmd = [program, *(["-ft4"] if mode == "ft4" else []), path]
        nice = shutil.which("nice")
        try:                                         # (the sound comes first)
            done = subprocess.run([nice, "-n", "10", *cmd] if nice else cmd, capture_output=True, text=True, timeout=DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError("the decoder took too long") from None
        except OSError as e:
            raise RuntimeError(f"the decoder wouldn't start: {e}") from None
    return parse(done.stdout)


class Reader:
    """Hears a receiver's sound and reads the FT8 (or FT4) in it, a slot at a time: feed() it blocks as
    they're played, step() it a few times a second, ask status() what it has read."""

    def __init__(self, clock: Callable[[], float] = time.time, decode: Callable[[np.ndarray, str], list[dict]] = run_decoder,
                 store: heard_mod.Store | None = None) -> None:
        self.on = False
        self.mode = "ft8"
        self.band: str | None = None                 # what the page says the receiver is on ("40 m"): the heard list is kept by it
        self.store = store if store is not None else heard_mod.Store(clock)     # who's been heard (the radio's reader: everyone's list)
        self.dial: int | None = None                 # where the receiver is tuned (Hz), if the page says: a message's place on the air is this and its pitch
        self.own: str | None = None                  # the receiver, if it's one of the owner's own ("http://192.168.50.4:8075"): only those are reported
        self.playing: Callable[[], str | None] | None = None     # what the radio is playing now (its address): the web side sets it
        self.report: Callable[[str, int, float, list[dict]], None] | None = None      # where what's read is reported (audio/report.py), if anywhere
        self._clock, self._decode = clock, decode
        self._lock = threading.Lock()
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._pieces: deque[np.ndarray] = deque()    # the sound kept, at RATE: as it came, put together when a slot is read
        self._kept = 0                               # ...how many samples that is
        self._end = 0.0                              # ...and when its last sample came, by the clock
        self._next: float | None = None              # the start of the next slot to read
        self._late: float | None = None              # how late the sound is, so where a slot is cut; None: not known, each tried
        self._empty = 0                              # slots in a row with nothing read
        self._n = 0
        self._messages: deque[dict] = deque(maxlen=400)
        self._slots = 0
        self._last: dict | None = None               # the slot last looked at: {"t", "heard" | "error"}
        self._turn = 0                               # (which switching-on this is: a slot read for the last one is dropped)

    # --- the sound coming in -----------------------------------------------------------------

    def feed(self, block: np.ndarray) -> None:
        """What's being played (a block at the speaker's rate): kept, with when it came. Cheap."""
        if not self.on:
            return
        now = self._clock()
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        with self._lock:
            mono = np.concatenate((self._left, mono)) if len(self._left) else mono
            whole = len(mono) // DECIMATE * DECIMATE
            self._left = mono[whole:]
            if whole:
                # When it came: by the count of samples since the last, drawn gently to the clock (blocks come in
                # fits and starts; the samples don't). A gap is a new start: what was kept is from another time.
                due = self._end + whole / pcm.SAMPLE_RATE
                if self._kept and abs(now - due) < GAP_S:
                    self._end = due + FOLLOW * (now - due)
                else:
                    if self._kept:                   # (...and its delay may be another)
                        self._late, self._empty = None, 0
                    self._forget_sound()
                    self._end = now
                piece = mono[:whole].reshape(-1, DECIMATE).mean(axis=1)
                self._pieces.append(piece)
                self._kept += len(piece)
                keep = int((KEEP_SLOTS * MODES[self.mode] + KEEP_MORE_S) * RATE)
                while self._kept - len(self._pieces[0]) >= keep:       # (the oldest goes, a piece at a time)
                    self._kept -= len(self._pieces.popleft())

    def _forget_sound(self) -> None:
        self._pieces.clear()
        self._kept = 0

    @property
    def _x(self) -> np.ndarray:
        """The sound kept, in one piece."""
        return np.concatenate(self._pieces) if self._pieces else np.zeros(0, np.float32)

    # --- what the page asks for --------------------------------------------------------------

    def status(self, since: int = 0) -> dict:
        """{"on", "mode", "band", "dial" (Hz, where the page said the receiver is tuned), "home" (the radio's locator), "can" (there's a decoder), "slot" (s), "slots" (read so
        far), "late" (s the sound is behind the clock, once known), "last": {"t", "heard" | "error"}, "messages": [{"n", "t" (the slot's start, by the clock), "snr", "dt",
        "hz", "text", "call"?, "grid"?, "km"?, "deg"?, "me"? (it names the owner's callsign)}] after number since,
        "call": the owner's callsign}"""
        with self._lock:
            own = own_call()
            return {"on": self.on, "mode": self.mode, "band": self.band, "dial": self.dial, "home": HOME, "call": own, "can": decoder_path() is not None, "can_wspr": wspr_decoder_path() is not None,
                    "slot": MODES[self.mode], "slots": self._slots, "late": None if self._late is None else round(self._late, 2), "last": self._last,
                    "messages": [_mine(_far(m), own) for m in self._messages if m["n"] > since]}

    def place(self, band: str | None, dial: int | None, own: str | None) -> None:
        """Where the receiver is, as the page says it (POST /api/rx/where, the one message every reader is told
        by): the band (the heard list is kept by it), where it's tuned (Hz) and, if it's the owner's own, its
        address (reports())."""
        with self._lock:
            self.band = " ".join(str(band).split())[:8] or None if band else None
            self.dial, self.own = (int(dial), own or None) if self.band and dial else (None, None)

    def heard(self) -> dict:
        """{"band", "home", "heard": [{"band", "call", "grid"?, "km"?, "deg"?, "snr" (the best), "first", "last" (by the
        clock), "count", "cq" (it was calling CQ when last heard)}], the latest heard first}"""
        return {"band": self.band, "home": HOME, "heard": [_far(h) for h in self.store.list({"wsjt"})]}

    def reports(self) -> bool:
        """Is what's read now fit to report? Only from a receiver of the owner's own, tuned where the page said,
        and still the one the radio is playing."""
        if self.report is None or not self.own or not self.dial or self.playing is None:
            return False
        from sleepradiopi.playback import calibrate   # (a receiver being calibrated is taken round the bands: not where the page said)
        from sleepradiopi.playback import survey      # (...nor one being surveyed)
        if calibrate.job.status().get("running") or survey.job.status().get("running"):
            return False
        return (self.playing() or "").startswith(self.own)

    def forget(self) -> None:
        """The heard list emptied (of FT8, FT4 and WSPR), on the card too."""
        self.store.forget({"wsjt"})

    # --- the heard list, kept (audio/heard.py) ------------------------------------------------

    def keep_in(self, file: Path | None) -> None:
        """Where the heard list is kept from now on, and what's there read back in."""
        self.store.keep_in(file)

    def save(self) -> None:
        """The heard list written to the card, if it has changed."""
        self.store.save()

    # --- the reading -------------------------------------------------------------------------

    def step(self) -> None:
        """Read the slot that has just ended, if one has and its sound is all here."""
        slot = MODES[self.mode]
        now = self._clock()
        if self._next is None:                       # (just switched on: from the next slot to begin)
            self._next = math.ceil(now / slot) * slot
        t0 = self._next
        used = USED_S.get(self.mode, slot)
        cuts = (WSPR_LATE_S,) if self.mode == "wspr" else (self._late,) if self._late is not None else SEARCH_LATE_S
        if now < t0 + cuts[-1] + used + WAIT_S:
            return
        self._next = t0 + slot
        if now > t0 + cuts[-1] + 2 * slot:           # (the radio was held up: that one's gone, on from now)
            self._next = math.ceil(now / slot) * slot - slot
            return
        with self._lock:
            x, end, turn = self._x, self._end, self._turn
        found, cut, heard_any = [], None, False
        try:
            for late in cuts:
                a = len(x) - int(round((end - (t0 + late)) * RATE))     # where the slot begins in what's kept
                b = a + int(used * RATE)
                got = x[max(a, 0):max(min(b, len(x)), 0)]
                if len(got) < ENOUGH * used * RATE:  # (nothing playing for part of it)
                    continue
                if a < 0:
                    got = np.concatenate((np.zeros(-a, np.float32), got))
                heard_any = True
                read = self._decode(to_decoder_rate(got), self.mode)
                if cut is None or len(read) > len(found):
                    found, cut = read, late
        except RuntimeError as e:
            with self._lock:
                if turn == self._turn:
                    self._last = {"t": t0, "error": str(e)}
            return
        if not heard_any:
            return
        if self.mode == "wspr":
            pass                                     # (its cut stays put)
        elif found:                                  # the cut moved to where the messages say it should be
            off = float(np.median([m["dt"] for m in found])) - ON_TIME_DT
            self._late, self._empty = min(LATE_MAX_S, max(0.0, cut + off)), 0
        else:
            self._empty += 1
            if self._empty >= LOST_AFTER:
                self._late = None
        with self._lock:
            if turn != self._turn:
                return
            self._slots += 1
            self._last = {"t": t0, "heard": len(found)}
            told = []
            mode, dial, band = self.mode, self.dial, self.band
        for m in found:
            call, grid, cq = (m.get("call"), m.get("grid"), False) if mode == "wspr" else who(m["text"])
            if call:
                h = self.store.add(mode, band, call, t0, m["snr"], grid, cq, dial + m["hz"] if dial else None)
                m = {**m, "call": call, **({"grid": h["grid"]} if "grid" in h else {})}
                told.append(m)
            with self._lock:
                if turn != self._turn:
                    return
                self._n += 1
                self._messages.append({"n": self._n, "t": t0, **m})
        if told and self.reports():
            self.report(mode, dial, t0, told)


def _far(d: dict) -> dict:
    """A message or a station heard, with how far and which way if its locator and the radio's are known."""
    if HOME and d.get("grid"):
        km, deg = path(HOME, d["grid"])
        return {**d, "km": km, "deg": deg}
    return d


def own_call() -> str | None:
    """The owner's callsign (the one kept for the rooms), or None."""
    from sleepradiopi.playback import room
    return room.CALLSIGN or None


def _mine(m: dict, own: str | None) -> dict:
    """A message, marked "me" if it names the owner's callsign (with or without a /P or a prefix)."""
    if own and any(own in w.strip("<>").split("/") for w in m["text"].upper().split()):
        return {**m, "me": True}
    return m


class Live(Reader):
    """The Reader as the radio uses it: stepped by a thread of its own while it's on."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._switching = threading.Lock()
        self._stop = threading.Event()

    def switch(self, on: bool, mode: str | None = None, band: str | None = None, dial: int | None = None, own: str | None = None) -> None:
        """On or off, FT8, FT4 or WSPR, and the band the receiver is on if the page says: with where it's tuned
        (Hz) and, if it's one of the owner's own, its address (see reports()). On (or another mode) starts from
        nothing, but for who's been heard. ValueError: no such mode."""
        on, mode = bool(on), (mode or self.mode)
        if mode not in MODES:
            raise ValueError("mode is ft8, ft4 or wspr")
        with self._switching:
            if band is not None:
                with self._lock:
                    self.band = " ".join(str(band).split())[:8] or None
                    self.dial, self.own = (int(dial), own or None) if self.band and dial else (None, None)
            if on == self.on and mode == self.mode:
                return
            self._stop.set()
            with self._lock:
                if band is None:                     # (another mode, or off: where it was tuned no longer holds)
                    self.dial, self.own = None, None
                self.on, self.mode = on, mode
                self._left = np.zeros(0, np.float32)
                self._forget_sound()
                self._next, self._last = None, None
                self._late, self._empty = None, 0
                self._turn += 1
                if on:
                    self._messages.clear()
                    self._slots = 0
            if not on:
                self.save()
            if on:
                self._stop = stop = threading.Event()
                threading.Thread(target=self._run, args=(stop,), name="ft8-reader", daemon=True).start()

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(STEP_S):
            try:
                self.step()
                self.store.tick()
            except Exception:                        # (a reading that falls over mustn't end the reader)
                log.exception("ft8: reading failed")


reader = Live(store=heard_mod.store)
