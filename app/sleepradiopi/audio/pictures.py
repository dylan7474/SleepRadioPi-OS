"""SSTV pictures and weather charts, watched for and read as they come.

audio/sstv.py reads a picture out of sound already held. This is the part
that keeps listening to what a receiver plays:

  * SSTV: the sound's pitch is followed all the time, and the last second
    or so of it looked through for a picture's header. When one is found
    the picture is read a line at a time as its sound arrives, so it grows
    on the page as it's sent. With no header, a picture already under way
    is known by its sync pulses (sstv.find_late: a few lines' worth, so
    the last LATE_KEEP_S of pitch are kept) and begun from there; it ends
    when they stop.
  * Hellschreiber: text sent as a picture of itself, a keyed tone. The
    strongest tone there is found and its strength followed; when it's
    being keyed a tape is begun, which grows a strip of text at a time
    (the strip being printed is passed on afresh each second) and ends when
    the tone has been quiet for a while.
  * Weather fax: a chart's start tone is waited for; the phasing lines after
    it say where a line begins; then lines are read, two a second, until
    the stop tone. A chart already under way can be begun by hand (its
    lines then start wherever they happen to, so it's wrapped round until
    the next one).

Pictures are kept, the last few (KEPT), as they were read: the web side
makes PNG files of them when the page asks. A finished one is also written
to the card as a PNG (keep_in() says where; once, when it's finished, and
again if it's slid), with a list of what they are, and read back at
start-up: a restart doesn't lose them.

Two things done to a picture afterwards:

  * clean(): speckle from a weak signal taken out -- each dot becomes the
    middle one of the nine about it. Asked for a look at a time (the
    picture kept is as it was read).
  * slide(): a chart begun by hand has its lines start wherever they
    happened to, so its left edge is somewhere in the middle. Told where
    along a line the edge really is, the chart is turned round to put it
    there: what's been read and, if it's still coming, what's to come.

`reader` is the one the radio uses: the station feeds it what a receiver
plays (broadcast/station.py), the web side switches it. As with Morse, the
reading is done in a process of its own at low priority, which passes back
only each new line.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import struct
import threading
import time
import zlib
from collections import deque
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm, sstv
from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

MODES = {"sstv": "SSTV", "fax": "Weather fax", "hell": "Hellschreiber"}
HELL_TONE_S = 3.0            # Hellschreiber: the sound a tone is looked for in
HELL_LOST_S = 20.0           # ...and with nothing printed for this long it's looked for afresh (the receiver may have been moved)
DECIMATE = 4                 # of the speaker's rate: 11025 samples a second (a dot of the quickest mode is two of them)
RATE = pcm.SAMPLE_RATE / DECIMATE
STEP_S = 1.0                 # the sound is looked at this often
MAX_PENDING = 600            # blocks fed and not yet taken, at most (half a minute)
TAIL = 512                   # samples of sound kept from one look to the next, for the filter (it must be even)
IDLE_KEEP_S = 6.0            # pitch kept while nothing is being read: a header is under a second long
LATE_KEEP_S = 7.0            # ...and for SSTV this much: five lines of the slowest mode, for a picture joined late
KEPT = 8                     # pictures kept
INDEX = "pictures.json"      # the list of those on the card, beside them
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


def unpng(data: bytes) -> np.ndarray:
    """A PNG file as png() writes them -> the picture (lines x width, or lines x width x 3). ValueError if it's
    any other kind of PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    at, head, body = 8, None, b""
    while at + 8 <= len(data):
        n, kind = struct.unpack(">I", data[at:at + 4])[0], data[at + 4:at + 8]
        chunk = data[at + 8:at + 8 + n]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            body += chunk
        at += 12 + n
    if head is None or head[2] != 8 or head[3] not in (0, 2) or head[6] != 0:
        raise ValueError("not a picture of ours")
    w, h, c = head[0], head[1], 1 if head[3] == 0 else 3
    rows = np.frombuffer(zlib.decompress(body), np.uint8)
    if len(rows) != h * (1 + w * c):
        raise ValueError("not a picture of ours")
    rows = rows.reshape(h, 1 + w * c)
    if rows[:, 0].any():                             # (every line "no filter", as png() writes them)
        raise ValueError("not a picture of ours")
    a = rows[:, 1:]
    return a.copy() if c == 1 else a.reshape(h, w, 3).copy()


def clean(picture: np.ndarray) -> np.ndarray:
    """Speckle taken out: each dot (each colour of it) becomes the middle one of the nine about it."""
    a = np.asarray(picture, np.uint8)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return a
    pad = np.pad(a, ((1, 1), (1, 1)) + ((0, 0),) * (a.ndim - 2), mode="edge")
    h, w = a.shape[:2]
    return np.median(np.stack([pad[i:i + h, j:j + w] for i in range(3) for j in range(3)]), axis=0).astype(np.uint8)


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
        self._after = 0                              # samples (since switched on) up to which a picture has been read: no other is looked for in them
        self._ended = False                          # a picture was ended by hand: what's left of it isn't begun again as one "joined late"
        self._raw = np.zeros(0, np.float32)          # Hellschreiber: the last few seconds of sound, to find the tone in...
        self._tone: float | None = None              # ...its pitch, once found...
        self._heard = 0                              # ...how many samples of sound have come in all...
        self._inked = 0                              # ...and when (in those) the tone was last found keyed
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
        """"begin": a chart now, without waiting for its start tone; "end": the picture being read is finished here;
        "slide:0.37": the chart being read has its lines begin that far along (it's turned round to match)."""
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
            self._sent = min(self._sent, pic.done, getattr(pic, "again", pic.done))      # (a picture joined late loses its last lines when its
                                                     # sync is found to have stopped; a tape's last strip is drawn afresh as it's printed)
            rows = pic.rgb[self._sent:pic.done]
            self._news.append({"n": self._n, "name": pic.name + (" (joined late)" if getattr(pic, "late", False) else ""), "w": pic.w,
                               "h": 0 if isinstance(pic, sstv.Fax) or getattr(pic, "late", False) else pic.h,
                               "grey": rows.ndim == 2, "from": self._sent, "rows": rows.tobytes(), "done": done})
            self._sent = pic.done
        if done:
            self._pic, self._fax = None, None
            self._after = self._at + len(self._f)    # (its last lines' sync pulses are still in the record: not another picture)
            self._say("listening")

    def step(self) -> None:
        """Take in the sound that's arrived, and read what can be read of it."""
        with self._lock:
            new, self._pending = self._pending, []
            orders, self._orders = self._orders, []
        if new and self.mode == "hell":              # (the record is the tone's strength, not the sound's pitch)
            fresh = np.concatenate(new)
            self._raw = np.concatenate((self._raw, fresh))[-int(HELL_TONE_S * RATE):]
            if self._tone is None or (self._pic is None and self._heard - self._inked > HELL_LOST_S * RATE):
                self._tone, self._inked = sstv.hell_tone(self._raw, RATE), self._heard
            x = np.concatenate([self._tail, fresh])
            a = sstv.hell_strength(x, RATE, self._tone, self._heard - TAIL) if self._tone is not None else np.zeros(len(x))
            self._f = np.concatenate((self._f, a[TAIL // 2:len(x) - TAIL // 2].astype(np.float32)))
            self._tail = x[-TAIL:]
            self._heard += len(fresh)
        elif new:
            x = np.concatenate([self._tail, *new])
            f, _ = sstv.pitch(x, RATE)
            self._f = np.concatenate((self._f, f[TAIL // 2:len(x) - TAIL // 2].astype(np.float32)))
            self._tail = x[-TAIL:]
        f = self._f
        if "end" in orders and self._pic is not None:
            self._ended = isinstance(self._pic, sstv.Picture)
            return self._finish()
        for o in orders:
            if o.startswith("slide:") and isinstance(self._pic, sstv.Fax):
                self._pic.rephase(float(o[6:]) * sstv.FAX_LINE_S)
                self._sent = 0                       # (all of it sent again, as it is now)
        if "begin" in orders and self._pic is None and self.mode == "fax":
            self._begin(sstv.Fax(max(0, len(f) - int(0.5 * RATE)), RATE))
        if self._pic is None:
            {"fax": self._watch_fax, "hell": self._watch_hell}.get(self.mode, self._watch_sstv)(f)
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
        keep_from = self._pic.reach() if self._pic is not None else max(0, len(self._f) - int((LATE_KEEP_S if self.mode == "sstv" else IDLE_KEEP_S) * RATE))
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
            return self._begin(sstv.Picture(found[0], found[1], RATE))
        if self._ended:                              # (one was just ended by hand: nothing until its pulses have stopped, wherever in the record)
            self._ended = sstv.find_late(f, RATE) is not None
            self._after = self._at + len(f)
            return
        a = max(0, self._after - self._at)
        late = sstv.find_late(f[a:], RATE)           # no header: one already under way, by its sync pulses
        if late is not None:
            self._begin(sstv.Picture(late[0], a + late[1], RATE, late=True, odd=late[2]))

    def _watch_hell(self, a: np.ndarray) -> None:
        """A tape is begun when the tone is being keyed: its best moments of the last two seconds well over its quiet ones."""
        n = int(2.0 * RATE)
        if self._tone is None or len(a) < n:
            return
        late = a[-n:]
        if float(np.percentile(late, 97)) > sstv.HELL_INK * max(float(np.percentile(late, 30)), 1e-9):
            self._inked = self._heard
            self._begin(sstv.Hell(max(0, len(a) - n), RATE))

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
        self._pictures: deque[dict] = deque(maxlen=KEPT)      # {"n", "name", "w", "h", "grey", "rows": bytearray, "t", "done", "file"?}
        self._folder: Path | None = None             # where finished ones are kept on the card (keep_in())
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
            cut_short = [p for p in self._pictures if not p["done"]]
            for p in cut_short:                      # (one still coming is the picture as far as it got)
                p["done"], p["h"] = True, len(p["rows"]) // (p["w"] * (1 if p["grey"] else 3))
            threading.Thread(target=lambda: [self._save(p) for p in cut_short if p["h"]], name="picture-keep", daemon=True).start() if cut_short else None
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

    # --- the pictures, kept on the card ------------------------------------------------------

    def keep_in(self, folder: Path | None) -> None:
        """Where finished pictures are kept from now on, and those there read back in (the newest KEPT of them)."""
        self._folder = folder
        if folder is None:
            return
        try:
            listed = json.loads((folder / INDEX).read_text())["pictures"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("pictures: the list in %s can't be read (%s): beginning again", folder, e)
            return
        back = []
        for item in listed[-KEPT:]:
            try:
                a = unpng((folder / str(item["file"])).read_bytes())
                back.append({"n": 0, "name": str(item["name"]), "w": a.shape[1], "h": a.shape[0], "grey": a.ndim == 2, "rows": bytearray(a.tobytes()),
                             "t": float(item["t"]), "done": True, "file": str(item["file"])})
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("pictures: %s can't be read back (%s)", item, e)
        with self._lock:
            for n, p in enumerate(back, 1):
                p["n"] = n
            self._pictures = deque(back, maxlen=KEPT)
            self._n0 = len(back)

    def _save(self, p: dict | None = None) -> None:
        """A finished picture written to the card (p), the list of them brought up to date, and the files of
        those no longer kept taken away."""
        folder = self._folder
        if folder is None:
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if p is not None:
                with self._lock:
                    c = 1 if p["grey"] else 3
                    a = np.frombuffer(bytes(p["rows"]), np.uint8).reshape((p["h"], p["w"]) if c == 1 else (p["h"], p["w"], 3))
                    file = p.setdefault("file", f"picture-{int(p['t'])}-{p['n']}.png")
                data = png(a)
                tmp = folder / (file + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, folder / file)
            with self._lock:
                kept = [{"file": q["file"], "name": q["name"], "t": q["t"]} for q in self._pictures if q.get("file")]
            write_atomic(folder / INDEX, json.dumps({"pictures": kept}))
            have = {k["file"] for k in kept}
            for f in folder.glob("picture-*.png"):
                if f.name not in have:
                    f.unlink(missing_ok=True)
        except OSError as e:
            log.warning("pictures: not kept on the card (%s)", e)

    def slide(self, n: int, along: float) -> None:
        """Picture n (a chart) has its lines begin `along` of the way across (0 to 1): it's turned round to put
        that at the left edge. ValueError if there's no such chart."""
        if isinstance(along, bool) or not isinstance(along, (int, float)) or not 0.0 <= along < 1.0:
            raise ValueError("where a line begins is a number from 0 to 1: how far across the picture")
        with self._lock:
            p = next((p for p in self._pictures if p["n"] == n), None)
            if p is None or not p["grey"]:
                raise ValueError("there's no such chart")
            conn = self._conn if not p["done"] else None
            if p["done"]:
                shift = int(round(along * p["w"])) % p["w"]
                a = np.frombuffer(bytes(p["rows"]), np.uint8).reshape(-1, p["w"])
                p["rows"] = bytearray(np.roll(a, -shift, axis=1).tobytes())
        if conn is not None:                         # (still coming: the reader turns it, and what's to come)
            try:
                conn.send(f"slide:{along:.5f}")
            except OSError:
                pass
        elif p.get("file"):
            self._save(p)

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
        [{"n", "name", "w", "h" (0 until a chart ends), "lines" (read so far), "done", "t", "grey" (a chart: it can
        be slid), "kept" (it's on the card)}], the newest last}"""
        with self._lock:
            return {"on": self.on, "mode": self.mode, "state": self._state,
                    "pictures": [{"n": p["n"], "name": p["name"], "w": p["w"], "h": p["h"], "lines": len(p["rows"]) // (p["w"] * (1 if p["grey"] else 3)),
                                  "done": p["done"], "t": p["t"], "grey": p["grey"], "kept": bool(p.get("file"))} for p in self._pictures]}

    def picture(self, n: int) -> np.ndarray | None:
        """Picture n as it stands: lines x width (greys) or lines x width x 3. (clean() takes the speckle out.)"""
        with self._lock:
            p = next((p for p in self._pictures if p["n"] == n), None)
            if p is None:
                return None
            c = 1 if p["grey"] else 3
            lines = len(p["rows"]) // (p["w"] * c)
            a = np.frombuffer(bytes(p["rows"][:lines * p["w"] * c]), np.uint8)
        return a.reshape(lines, p["w"]) if c == 1 else a.reshape(lines, p["w"], 3)

    def _take(self, item: dict, n0: int) -> dict | None:
        """What the reader passed back, taken in. -> the picture, if this finished it (to be kept on the card)."""
        if "state" in item:
            self._state = item["state"]
            return None
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
                return None
            return p
        return None

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
                        finished = self._take(item, n0)
                    if finished is not None:
                        self._save(finished)
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
