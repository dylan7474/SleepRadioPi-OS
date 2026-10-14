"""PSK31 and RTTY followed over time: what's typed on the air, as it's typed.

audio/psk.py and audio/rtty.py each read a stretch of sound. This is the
part that keeps listening: the last few seconds are read again every second
or two, and what's new since the last look is passed on.

  * PSK31 and PSK63: every signal in what the receiver hears, at once (up
    to PSK_MOST). Each is a track, known by its pitch and its speed; a track
    nothing has come from for a while is dropped, and a new one is believed
    only if it spells (psk.spells(): the one rule the survey's finder and
    What's this? go by too). A new signal is tried at each speed
    (read at the wrong one it says nothing: see audio/psk.py), and, if it
    says nothing as either, as QPSK31, the four-phase kind, each way up.
  * RTTY: every signal there, too (up to RTTY_MOST: in a contest there are
    a dozen side by side), each a track known by its tones. Once one
    frames, its tones, speed and sense are kept (so reading on costs a
    sixth of finding), and it's dropped when it has said nothing for a
    while. Finding is the dear part, so only RTTY_TRY new pairs of tones
    are tried each time, the strongest untried first; one that didn't
    frame is left a few seconds (it may only just have begun), and twice
    as long each time it fails again. Two signals can't share a stretch
    of the band: the keying of a wide one (the weather broadcasts' tones
    are 450 Hz apart) puts energy between its tones that looks like a
    narrow pair, and frames, and reads as rubbish (seen on the German
    weather service). So a pair inside a signal being read is left alone,
    and a wider one that frames takes over from a narrower one inside it.

What's read comes out in pieces: {"n", "hz", "t0", "t1" (seconds of sound
since switched on), "text"}; the page puts a signal's pieces together on a
line (web/desktop.html: the PSK and RTTY keys).

`reader` is the one the radio uses: the station feeds it what a receiver
plays (broadcast/station.py), the web side switches it and asks what it has
read. As with Morse, the reading is done in a process of its own at low
priority, so it never takes the processor from the thread playing the sound.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
from collections import deque

import numpy as np

from sleepradiopi.audio import heard as heard_mod, pcm, psk, rtty

log = logging.getLogger(__name__)

MODES = {"psk31": "PSK31", "rtty": "RTTY"}
DECIMATE = 6                 # of the speaker's rate: 7350 samples a second, as the Morse reader has it
RATE = pcm.SAMPLE_RATE / DECIMATE
WINDOW_S = 8.0               # the stretch read each time
MARGIN_S = 1.0               # ...but for its last second: read next time, with what follows it
AGAIN_S = 0.05               # a letter this soon after the last one passed on is that one, read again a shade later
KEEP_S = 12.0                # audio kept
STEP_S = 1.5                 # the sound is looked at this often
MAX_PENDING = 600            # blocks fed and not yet taken, at most (half a minute)
PSK_MOST = 6                 # PSK31 signals read at once
PSK_SAME_HZ = 10.0           # a signal within this of a track is that track
PSK_FIRST = 4                # letters a new signal must say for its speed (or the four-phase kind) to be the one tried; believed: psk.spells()
TRACK_S = 20.0               # a track nothing has come from for this long is dropped
RTTY_LOST_S = 12.0           # an RTTY signal that has said nothing for this long is dropped (and looked for afresh)
RTTY_MOST = 4                # RTTY signals read at once
RTTY_TRY = 1                 # new pairs of tones tried each time (finding one costs six readings)
RTTY_GAP_HZ = 20.0           # two signals' tones are at least this far from each other's stretch of the band
RTTY_SAME_HZ = 40.0          # a pair of tones within this of a track's, the same distance apart, is that track
RTTY_AGAIN_S = 3.0           # a pair that didn't frame isn't tried again for this long (it may have just begun)...
RTTY_AGAIN_MOST_S = 24.0     # ...twice as long each time it fails again, up to this


class Reader:
    """Hears a receiver's sound and reads the PSK31 or RTTY in it: feed() it blocks as they're played (or
    take() sound already at RATE), step() it every STEP_S, ask status() what it has read."""

    def __init__(self, mode: str = "psk31") -> None:
        self.on = False
        self.mode = mode
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] = []
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._x = np.zeros(0, np.float32)            # the audio kept, at RATE
        self._x0 = 0                                 # ...and where it starts, in samples since switched on
        self._n = 0
        self._pieces: deque[dict] = deque(maxlen=200)
        self._tracks: list[dict] = []                # PSK31: {"hz", "done" (s read up to), "last" (s something came), "eye"}
        self._rttys: list[dict] = []                 # RTTY: {"low", "shift", "baud", "mark_high", "done", "last", "figs", "framed"}
        self._tried: dict[tuple[int, int], tuple[float, int]] = {}     # RTTY: pairs of tones that didn't frame -> (when, how many times)
        self._at = 0.0
        self._signals: list[dict] = []               # what's being read now, for the page

    # --- the sound coming in (as the Morse reader takes it) ------------------------------------

    def feed(self, block: np.ndarray) -> None:
        """What's being played (a block at the speaker's rate): kept for the reading thread. Cheap."""
        if not self.on:
            return
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        with self._lock:
            mono = np.concatenate((self._left, mono)) if len(self._left) else mono
            whole = len(mono) // DECIMATE * DECIMATE
            self._left = mono[whole:]
            if whole:
                self._pending.append(mono[:whole].reshape(-1, DECIMATE).mean(axis=1))
                del self._pending[:-MAX_PENDING]     # (nobody taking it: the oldest goes)

    def take(self, sound: np.ndarray) -> None:
        """Sound already at RATE."""
        with self._lock:
            self._pending.append(np.asarray(sound, np.float32))

    def drain(self) -> np.ndarray | None:
        """What's been fed and not yet taken, at RATE (for a Reader that only passes it on)."""
        with self._lock:
            new, self._pending = self._pending, []
        return np.concatenate(new) if new else None

    # --- what the page asks for --------------------------------------------------------------

    def status(self, since: int = 0) -> dict:
        """{"on", "mode", "at" (s of sound heard), "signals": [{"hz", ...}] (being read now: PSK with "baud" 31 or 63,
        RTTY with "shift", "baud", "mark_high", "framed"), "pieces": [{"n", "hz", "baud"? (PSK: 31 or 63), "qpsk"? (the four-phase kind), "t0", "t1",
        "text"}] after number since}"""
        with self._lock:
            return {"on": self.on, "mode": self.mode, "at": round(self._at, 2), "signals": list(self._signals),
                    "pieces": [p for p in self._pieces if p["n"] > since]}

    # --- the reading -------------------------------------------------------------------------

    def step(self) -> None:
        """Take in the sound that's arrived, and pass on what's new in it."""
        with self._lock:
            new, self._pending = self._pending, []
        if new:
            self._x = np.concatenate([self._x, *new])
            over = len(self._x) - int(KEEP_S * RATE)
            if over > 0:
                self._x, self._x0 = self._x[over:], self._x0 + over
        x = self._x[-int(WINDOW_S * RATE):]
        if len(x) < RATE * 3:
            return
        t0 = (self._x0 + len(self._x) - len(x)) / RATE           # when x begins, s since switched on
        end = t0 + len(x) / RATE
        pieces, signals = (self._psk if self.mode == "psk31" else self._teleprinter)(x, t0, end)
        with self._lock:
            self._at, self._signals = end, signals
            for p in pieces:
                self._n += 1
                self._pieces.append({"n": self._n, **p})

    def _psk(self, x: np.ndarray, t0: float, end: float) -> tuple[list[dict], list[dict]]:
        upto = end - MARGIN_S
        self._tracks = [t for t in self._tracks if end - t["last"] < TRACK_S]
        pitches = [(t["hz"], t) for t in self._tracks]
        for hz, _ in psk.find(x, RATE, most=PSK_MOST * 2):
            if all(abs(hz - p) > psk.APART * 0.75 for p, _ in pitches) and len(pitches) < PSK_MOST * 2:
                pitches.append((hz, None))
        pieces, seen = [], []
        for hz, track in pitches:
            if track is not None:
                r = psk.read_qpsk(x, RATE, hz, track["baud"], track["reverse"]) if track.get("qpsk") else psk.read(x, RATE, hz, track["baud"])
            else:                                    # a new one: which speed is it? The one that says the more
                r = max((psk.read(x, RATE, hz, baud) for baud in psk.BAUDS), key=lambda r: len(r["chars"]))
                if len(r["chars"]) < PSK_FIRST:      # ...or neither: the four-phase kind, then, one way up or the other
                    q = max((psk.read_qpsk(x, RATE, hz, reverse=rev) for rev in (False, True)), key=lambda r: len(r["chars"]))
                    if len(q["chars"]) >= PSK_FIRST:
                        r = q
            if any(abs(r["pitch"] - s) < PSK_SAME_HZ for s in seen):      # (two finds of the one signal)
                continue
            if track is None:
                track = next((t for t in self._tracks if abs(t["hz"] - r["pitch"]) < PSK_SAME_HZ), None)
            done = track["done"] if track else t0
            chars = [(c, t0 + when) for c, when, _ in r["chars"] if done + AGAIN_S < t0 + when <= upto]
            if track is None:
                if not psk.spells(r) or len(self._tracks) >= PSK_MOST:
                    continue                         # (the one rule: the finder and What's this? go by it too)
                track = {"hz": r["pitch"], "baud": r["baud"], "done": t0, "last": end, "eye": r["eye"],
                         **({"qpsk": True, "reverse": r["reverse"]} if r.get("qpsk") else {})}
                self._tracks.append(track)
            seen.append(r["pitch"])
            if r["chars"]:
                track.update(hz=r["pitch"], eye=r["eye"])
            if chars:
                track.update(done=chars[-1][1], last=end)
                pieces.append({"hz": round(track["hz"]), "baud": psk.NAMES[track["baud"]], **({"qpsk": True} if track.get("qpsk") else {}),
                               "t0": round(chars[0][1], 2), "t1": round(chars[-1][1], 2), "text": "".join(c for c, _ in chars)})
            track["done"] = max(track["done"], t0)   # (what has gone from the window is done with)
        return pieces, [{"hz": round(t["hz"]), "baud": psk.NAMES[t["baud"]], "eye": round(t["eye"], 2), **({"qpsk": True} if t.get("qpsk") else {})}
                        for t in sorted(self._tracks, key=lambda t: t["hz"])]

    def _teleprinter(self, x: np.ndarray, t0: float, end: float) -> tuple[list[dict], list[dict]]:
        upto = end - MARGIN_S
        self._rttys = [s for s in self._rttys if end - s["last"] <= RTTY_LOST_S]
        self._tried = {k: v for k, v in self._tried.items() if end - v[0] < 2 * RTTY_AGAIN_MOST_S}

        def resting(key) -> bool:                    # tried, didn't frame, and not yet due another go
            at, fails = self._tried.get(key, (0.0, 0))
            return fails > 0 and end - at < min(RTTY_AGAIN_MOST_S, RTTY_AGAIN_S * 2 ** (fails - 1))
        pieces = []

        def said(s: dict, r: dict) -> None:          # what's new from a signal, passed on
            codes = [(c, t0 + when, sure) for c, when, sure in r["codes"] if s["done"] + AGAIN_S < t0 + when <= upto]
            if codes:
                chars, s["figs"] = rtty.text(codes, s["figs"])
                s.update(done=codes[-1][1], last=end)
                text = "".join(c for c, _, _ in chars).replace("\r", "")
                if text:
                    pieces.append({"hz": round(s["low"] + s["shift"] / 2), "t0": round(codes[0][1], 2), "t1": round(codes[-1][1], 2), "text": text})
                s["level"] += 0.3 * (r["level"] - s["level"])      # (how strong it is, followed: a fade is, a stop isn't)
            s["done"], s["framed"] = max(s["done"], t0), r["framed"]

        for s in self._rttys:                        # the ones being read: on from where they were
            said(s, rtty.read(x, RATE, s["low"], s["shift"], s["baud"], s["mark_high"], s["level"]))
        tries = 0                                    # ...and a new one, if there's a pair of tones that isn't one of them
        for low, shift, _ in rtty.find_all(x, RATE, most=RTTY_MOST * 2):
            key = (round(low / RTTY_SAME_HZ), round(shift))
            if tries >= RTTY_TRY:
                break
            across = [s for s in self._rttys if low < s["low"] + s["shift"] + RTTY_GAP_HZ and s["low"] < low + shift + RTTY_GAP_HZ]
            if resting(key) or any(s["shift"] >= shift for s in across) or (not across and len(self._rttys) >= RTTY_MOST):
                continue
            tries += 1
            r = rtty.read(x, RATE, low, shift)
            if not r["codes"]:
                self._tried[key] = (end, self._tried.get(key, (0.0, 0))[1] + 1)
                continue
            self._tried.pop(key, None)
            self._rttys = [s for s in self._rttys if not any(s is a for a in across)]      # (the narrower ones inside it were its own keying)
            s = {"low": low, "shift": shift, "baud": r["baud"], "mark_high": r["mark_high"], "done": t0, "last": end, "figs": False, "framed": r["framed"], "level": r["level"]}
            self._rttys.append(s)
            said(s, r)
        return pieces, [{"hz": round(s["low"] + s["shift"] / 2), "shift": round(s["shift"]), "baud": s["baud"], "mark_high": s["mark_high"], "framed": round(s["framed"], 2)}
                        for s in sorted(self._rttys, key=lambda s: s["low"])]


def _serve(conn, mode: str) -> None:
    """The reading process: sound in (arrays at RATE; None to stop), what's been read out, every STEP_S."""
    try:
        os.nice(10)                                  # (the sound comes first)
    except OSError:
        pass
    core, seen = Reader(mode), 0
    core.on = True
    try:
        while True:
            if conn.poll(STEP_S):
                while conn.poll():
                    sound = conn.recv()
                    if sound is None:
                        return
                    core.take(sound)
            try:
                core.step()
            except Exception:                        # (a reading that falls over mustn't end the reader)
                log.exception("keyboard: reading failed")
            st = core.status(seen)
            if st["pieces"]:
                seen = st["pieces"][-1]["n"]
            conn.send(st)
    except (EOFError, OSError, KeyboardInterrupt):   # the radio has gone
        return


class Remote:
    """The Reader as the radio uses it: the reading in a process of its own. feed() and status() are cheap
    and never wait for it."""

    def __init__(self) -> None:
        self.on = False
        self.mode = "psk31"
        self._lock = threading.Lock()
        self._inbox = Reader()                       # (only to bring the sound down to RATE and hold it till it's sent)
        self._stop = threading.Event()
        self._pieces: deque[dict] = deque(maxlen=200)
        self._n = 0                                  # (numbered here: they go on across a switch, so the page can tell a new start)
        self._now: dict = {"at": 0.0, "signals": []}
        self._calls = heard_mod.Text("psk31", heard_mod.store)  # (who sent what's read goes on the list of who's been heard)

    def switch(self, on: bool, mode: str | None = None) -> None:
        """On or off, PSK31 or RTTY. On (or the other mode) starts from nothing. ValueError: no such mode."""
        on, mode = bool(on), (mode or self.mode)
        if mode not in MODES:
            raise ValueError("mode is psk31 or rtty")
        with self._lock:
            if on == self.on and mode == self.mode:
                return
            self._stop.set()
            self.on, self.mode = on, mode
            self._inbox.on = False
            self._pieces.clear()
            self._now = {"at": 0.0, "signals": []}
            self._calls.clear()
            if not on:
                heard_mod.store.save()
                return
            self._inbox = Reader(mode)
            self._inbox.on = True
            conn, child = mp.get_context("spawn").Pipe()
            proc = mp.get_context("spawn").Process(target=_serve, args=(child, mode), name="keyboard-reader", daemon=True)
            proc.start()
            child.close()
            self._stop = threading.Event()
            threading.Thread(target=self._carry, args=(self._stop, conn, proc, self._inbox), name="keyboard-reader", daemon=True).start()

    def feed(self, block: np.ndarray) -> None:
        """What's being played (a block at the speaker's rate). Cheap: it's only kept for the carrying thread."""
        if self.on:
            self._inbox.feed(block)

    def status(self, since: int = 0) -> dict:
        """As Reader.status(): {"on", "mode", "at", "signals", "pieces": those after number since}."""
        with self._lock:
            return {"on": self.on, "mode": self.mode, **self._now, "pieces": [p for p in self._pieces if p["n"] > since]}

    def _carry(self, stop: threading.Event, conn, proc, inbox: Reader) -> None:
        """Sound to the reading process and what it has read back, until switched off."""
        try:
            while not stop.wait(0.4):
                sound = inbox.drain()
                if sound is not None:
                    conn.send(sound)
                while conn.poll():
                    st = conn.recv()
                    with self._lock:
                        if stop.is_set():
                            break
                        self._now = {"at": st["at"], "signals": st["signals"]}
                        for p in st["pieces"]:
                            self._n += 1
                            self._pieces.append({**p, "n": self._n})
                    heard_mod.store.tick()
                    for p in st["pieces"]:          # (a signal at a time: its pitch, to the nearest 20 Hz, tells it)
                        mode = "rtty" if self.mode == "rtty" else "qpsk31" if p.get("qpsk") else "psk63" if p.get("baud") == 63 else "psk31"
                        self._calls.take((mode, round(p["hz"] / 20)), p["text"], p["hz"], mode=mode)
        except (EOFError, OSError):
            log.error("keyboard: the reading process has gone (exit code %s)", proc.exitcode)
            with self._lock:
                if not stop.is_set():
                    self.on = inbox.on = False
        finally:
            try:
                conn.send(None)
            except OSError:
                pass
            proc.join(timeout=3)
            if proc.is_alive():
                proc.kill()
            conn.close()


reader = Remote()
