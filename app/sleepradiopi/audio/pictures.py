"""SSTV pictures and weather charts, watched for and read as they come.

audio/sstv.py reads a picture out of sound already held. This is the part
that keeps listening to what a receiver plays:

  * SSTV: the sound's pitch is followed all the time, and the last second
    or so of it looked through for a picture's header. When one is found
    the picture is read a line at a time as its sound arrives, so it grows
    on the page as it's sent.
  * Weather fax: a chart's start tone is waited for; the phasing lines after
    it say where a line begins; then lines are read, two a second, until
    the stop tone. A chart already under way can be begun by hand (its
    lines then start wherever they happen to, so it's wrapped round until
    the next one).

Pictures are kept in memory, the last few (KEPT), as they were read: the
web side makes PNG files of them when the page asks. Nothing is written to
the card.

`reader` is the one the radio uses: the station feeds it what a receiver
plays (broadcast/station.py), the web side switches it. As with Morse, the
reading is done in a process of its own at low priority, which passes back
only each new line.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import struct
import threading
import time
import zlib
from collections import deque

import numpy as np

from sleepradiopi.audio import pcm, sstv

log = logging.getLogger(__name__)

MODES = {"sstv": "SSTV", "fax": "Weather fax"}
DECIMATE = 4                 # of the speaker's rate: 11025 samples a second (a dot of the quickest mode is two of them)
RATE = pcm.SAMPLE_RATE / DECIMATE
STEP_S = 1.0                 # the sound is looked at this often
MAX_PENDING = 600            # blocks fed and not yet taken, at most (half a minute)
TAIL = 512                   # samples of sound kept from one look to the next, for the filter (it must be even)
IDLE_KEEP_S = 6.0            # pitch kept while nothing is being read: a header is under a second long
KEPT = 6                     # pictures kept
PHASING_S = 8.0              # of a chart's phasing lines looked at for where a line begins
START_WAIT_S = 40.0          # after a start tone: no phasing lines found in this long, and it's begun anyway


def png(picture: np.ndarray) -> bytes:
    """A picture (lines x width bytes: greys; or lines x width x 3: red, green, blue) as a PNG file."""
    a = np.ascontiguousarray(picture, np.uint8)
    if a.ndim == 2:
        a = a[:, :, None]
    h, w, c = a.shape
    if h == 0:
        a, h = np.zeros((1, w, c), np.uint8), 1
    rows = np.concatenate((np.zeros((h, 1), np.uint8), a.reshape(h, w * c)), axis=1)      # (each line begins with "no filter")
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0 if c == 1 else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + chunk(b"IEND", b""))


class Reader:
    """Hears a receiver's sound and reads the pictures in it: feed() it blocks as they're played (or take()
    sound already at RATE), step() it every STEP_S, drain what it has read with news()."""

    def __init__(self, mode: str = "sstv") -> None:
        self.on = False
        self.mode = mode
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] = []
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._tail = np.zeros(TAIL, np.float32)      # the end of the sound last looked at
        self._f = np.zeros(0, np.float32)            # the pitch kept, and...
        self._at = 0                                 # ...how many samples have gone before it
        self._pic = None                             # the picture being read: sstv.Picture or sstv.Fax
        self._n = 0                                  # pictures begun
        self._sent = 0                               # lines of this one passed on
        self._news: list[dict] = []
        self._fax: dict | None = None                # a chart announced: {"since": sample of the start tone's end or None, "seen": when}
        self._orders: list[str] = []
        self.state = "listening"

    # --- the sound coming in (as the Morse reader takes it) ------------------------------------

    def feed(self, block: np.ndarray) -> None:
        if not self.on:
            return
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        with self._lock:
            mono = np.concatenate((self._left, mono)) if len(self._left) else mono
            whole = len(mono) // DECIMATE * DECIMATE
            self._left = mono[whole:]
            if whole:
                self._pending.append(mono[:whole].reshape(-1, DECIMATE).mean(axis=1))
                del self._pending[:-MAX_PENDING]

    def take(self, sound: np.ndarray) -> None:
        with self._lock:
            self._pending.append(np.asarray(sound, np.float32))

    def drain(self) -> np.ndarray | None:
        with self._lock:
            new, self._pending = self._pending, []
        return np.concatenate(new) if new else None

    def order(self, what: str) -> None:
        """"begin": a chart now, without waiting for its start tone; "end": the picture being read is finished here."""
        with self._lock:
            self._orders.append(what)

    def news(self) -> list[dict]:
        """What's been read since last asked: [{"n", "name", "w", "h" (0: not known yet), "grey", "from" (the first
        line here), "rows" (bytes), "done"}], and {"state": ...} when what it's doing changes."""
        with self._lock:
            out, self._news = self._news, []
        return out

    # --- the reading -------------------------------------------------------------------------

    def _begin(self, pic) -> None:
        self._pic, self._sent = pic, 0
        self._n += 1
        self._say("receiving")

    def _say(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._news.append({"state": state})

    def _pass_on(self, done: bool) -> None:
        pic = self._pic
        rows = pic.rgb[self._sent:pic.done]
        if len(rows) or done:
            self._news.append({"n": self._n, "name": pic.name, "w": pic.w, "h": 0 if isinstance(pic, sstv.Fax) else pic.h,
                               "grey": rows.ndim == 2, "from": self._sent, "rows": rows.tobytes(), "done": done})
            self._sent = pic.done
        if done:
            self._pic, self._fax = None, None
            self._say("listening")

    def step(self) -> None:
        """Take in the sound that's arrived, and read what can be read of it."""
        with self._lock:
            new, self._pending = self._pending, []
            orders, self._orders = self._orders, []
        if new:
            x = np.concatenate([self._tail, *new])
            f, _ = sstv.pitch(x, RATE)
            self._f = np.concatenate((self._f, f[TAIL // 2:len(x) - TAIL // 2].astype(np.float32)))
            self._tail = x[-TAIL:]
        f = self._f
        if "end" in orders and self._pic is not None:
            return self._finish()
        if "begin" in orders and self._pic is None and self.mode == "fax":
            self._begin(sstv.Fax(max(0, len(f) - int(0.5 * RATE)), RATE))
        if self._pic is None:
            (self._watch_fax if self.mode == "fax" else self._watch_sstv)(f)
        if self._pic is not None:
            cum = np.concatenate(([0.0], np.cumsum(self._f, dtype=np.float64)))
            self._pic.read(self._f, cum)
            if isinstance(self._pic, sstv.Fax) and len(self._f) > RATE * 2 and sstv.fax_tone(self._f[-int(RATE):], RATE) == "stop":
                # (the stop tone: the lines read off it, the last few, aren't chart)
                self._pic.grey = self._pic.grey[:max(0, self._pic.done - 4)]
                self._pic.done = len(self._pic.grey)
                self._pic.finished = True
            self._pass_on(self._pic.finished)
        # what's no longer wanted of the pitch goes
        keep_from = self._pic.reach() if self._pic is not None else max(0, len(self._f) - int(IDLE_KEEP_S * RATE))
        if self._fax and self._fax.get("since") is not None:
            keep_from = min(keep_from, self._fax["since"])
        if keep_from > RATE:
            self._f = self._f[keep_from:]
            self._at += keep_from
            if self._pic is not None:
                self._pic.moved(keep_from)
            if self._fax and self._fax.get("since") is not None:
                self._fax["since"] -= keep_from

    def _finish(self) -> None:
        pic = self._pic
        if isinstance(pic, sstv.Picture):            # (cut short: what was read is the picture)
            pic.rgb = pic.rgb[:pic.done]
        self._pass_on(True)

    def _watch_sstv(self, f: np.ndarray) -> None:
        found = sstv.find_vis(f, RATE)
        if found is not None:
            self._begin(sstv.Picture(found[0], found[1], RATE))

    def _watch_fax(self, f: np.ndarray) -> None:
        if len(f) < RATE:
            return
        tone = sstv.fax_tone(f[-int(RATE):], RATE)
        if tone == "start":
            self._fax = {"since": None, "seen": len(f) + self._at}
            return self._say("announced")
        if self._fax is None:
            return
        if self._fax["since"] is None:               # the start tone has just ended: the phasing lines follow
            self._fax["since"] = max(0, len(f) - int(0.5 * RATE))
        got = len(f) - self._fax["since"]
        if got >= PHASING_S * RATE:
            phase = sstv.fax_phase(f[self._fax["since"]:self._fax["since"] + int(PHASING_S * RATE)], RATE)
            if phase is not None or got >= START_WAIT_S * RATE:
                self._begin(sstv.Fax(self._fax["since"] + (phase or 0.0) * RATE, RATE))


def _serve(conn, mode: str) -> None:
    """The reading process: sound in (arrays at RATE; a word: an order; None to stop), what's been read out."""
    try:
        os.nice(10)
    except OSError:
        pass
    core = Reader(mode)
    core.on = True
    try:
        while True:
            if conn.poll(STEP_S):
                while conn.poll():
                    got = conn.recv()
                    if got is None:
                        return
                    if isinstance(got, str):
                        core.order(got)
                    else:
                        core.take(got)
            try:
                core.step()
            except Exception:
                log.exception("pictures: reading failed")
            for item in core.news():
                conn.send(item)
    except (EOFError, OSError, KeyboardInterrupt):
        return


class Remote:
    """The Reader as the radio uses it: the reading in a process of its own; the pictures kept here."""

    def __init__(self) -> None:
        self.on = False
        self.mode = "sstv"
        self._lock = threading.Lock()
        self._inbox = Reader()
        self._stop = threading.Event()
        self._conn = None
        self._pictures: deque[dict] = deque(maxlen=KEPT)      # {"n", "name", "w", "h", "grey", "rows": bytearray, "t", "done"}
        self._n0 = 0                                 # (pictures are numbered on from one switching-on to the next)
        self._state = "listening"

    def switch(self, on: bool, mode: str | None = None) -> None:
        """On or off, SSTV or weather fax. The pictures read stay. ValueError: no such mode."""
        on, mode = bool(on), (mode or self.mode)
        if mode not in MODES:
            raise ValueError("mode is sstv or fax")
        with self._lock:
            if on == self.on and mode == self.mode:
                return
            self._stop.set()
            self.on, self.mode, self._state = on, mode, "listening"
            self._inbox.on = False
            self._n0 = max([p["n"] for p in self._pictures], default=self._n0)
            for p in self._pictures:
                p["done"] = True
            if not on:
                self._conn = None
                return
            self._inbox = Reader(mode)
            self._inbox.on = True
            conn, child = mp.get_context("spawn").Pipe()
            proc = mp.get_context("spawn").Process(target=_serve, args=(child, mode), name="picture-reader", daemon=True)
            proc.start()
            child.close()
            self._conn = conn
            self._stop = threading.Event()
            threading.Thread(target=self._carry, args=(self._stop, conn, proc, self._inbox, self._n0), name="picture-reader", daemon=True).start()

    def feed(self, block: np.ndarray) -> None:
        if self.on:
            self._inbox.feed(block)

    def order(self, what: str) -> None:
        """"begin" a chart now; "end" the picture being read."""
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.send(what)
            except OSError:
                pass

    def status(self) -> dict:
        """{"on", "mode", "state" ("listening", "announced": a chart's start tone heard, "receiving"), "pictures":
        [{"n", "name", "w", "h" (0 until a chart ends), "lines" (read so far), "done", "t"}], the newest last}"""
        with self._lock:
            return {"on": self.on, "mode": self.mode, "state": self._state,
                    "pictures": [{"n": p["n"], "name": p["name"], "w": p["w"], "h": p["h"], "lines": len(p["rows"]) // (p["w"] * (1 if p["grey"] else 3)),
                                  "done": p["done"], "t": p["t"]} for p in self._pictures]}

    def picture(self, n: int) -> np.ndarray | None:
        """Picture n as it stands: lines x width (greys) or lines x width x 3."""
        with self._lock:
            p = next((p for p in self._pictures if p["n"] == n), None)
            if p is None:
                return None
            c = 1 if p["grey"] else 3
            lines = len(p["rows"]) // (p["w"] * c)
            a = np.frombuffer(bytes(p["rows"][:lines * p["w"] * c]), np.uint8)
        return a.reshape(lines, p["w"]) if c == 1 else a.reshape(lines, p["w"], 3)

    def _take(self, item: dict, n0: int) -> None:
        if "state" in item:
            self._state = item["state"]
            return
        n = n0 + item["n"]
        p = next((p for p in self._pictures if p["n"] == n), None)
        if p is None:
            p = {"n": n, "name": item["name"], "w": item["w"], "h": item["h"], "grey": item["grey"], "rows": bytearray(), "t": time.time(), "done": False}
            self._pictures.append(p)
        width = p["w"] * (1 if p["grey"] else 3)
        del p["rows"][item["from"] * width:]         # (lines come in order: one sent again takes the place of what follows)
        p["rows"] += item["rows"]
        if item["done"]:
            p["done"] = True
            p["h"] = len(p["rows"]) // width
            if not p["h"]:
                self._pictures.remove(p)

    def _carry(self, stop: threading.Event, conn, proc, inbox: Reader, n0: int) -> None:
        try:
            while not stop.wait(0.4):
                sound = inbox.drain()
                if sound is not None:
                    conn.send(sound)
                while conn.poll():
                    item = conn.recv()
                    with self._lock:
                        if stop.is_set():
                            break
                        self._take(item, n0)
        except (EOFError, OSError):
            log.error("pictures: the reading process has gone (exit code %s)", proc.exitcode)
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
