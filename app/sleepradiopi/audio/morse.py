"""Morse (CW) read off a receiver's audio, as text.

What's playing from a receiver is listened to for a keyed tone; when there
is one it's read, an over at a time, and the desktop's rig window shows the
text with the dots and dashes it came from (web/desktop.html: the CW key).

How a signal is read:

  * A tight filter round the tone. The tone is moved to 0 Hz and low-passed
    to a width matched to the sender's speed (about 30 Hz at 22 words a
    minute), so a stronger station 150 Hz away makes no difference.
  * The best fit, not a threshold. The envelope is cut into dots, dashes and
    the three kinds of gap by the sequence that explains it best under
    Morse's own timing (a Viterbi search over segments): a dot that noise
    has split, or a gap a shaky fist has shortened, is still read right.
  * Fading followed: the signal's level is taken again from the marks just
    read, then it's read once more.
  * The speed found by trying each: the one whose strict-timing reading
    beats "nothing was sent" by most.
  * Each letter's confidence is how far its weakest mark stood above the
    noise. A reading that doesn't stand clear of the noise, or whose timing
    isn't Morse's (a teleprinter's tone looks keyed too), is left unsaid:
    nothing is better than rubbish.

Real traffic comes in overs: a three-second call, silence, then someone else
at another speed and a slightly different pitch. So each over is read on
its own, with its own pitch and speed, when it ends (or every ten seconds
or so of a long one, at a gap between words): text arrives an over at a
time, a second or so after the sender stops.

Measured with known Morse mixed into real 30 m noise (strength against the
noise in 2.5 kHz): clean down to -6 dB, shaky hand keying about 3% of
letters wrong, about 15% wrong at -9 dB, and nothing said at -12 dB. A good
ear still goes a few dB lower.

`reader` is the one Reader: the station feeds it what a receiver plays
(broadcast/station.py), the web side switches it and asks what it has read.
The reading is done on a thread of its own, never the one playing the sound.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

MORSE = {".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
         "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
         "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z", "-----": "0", ".----": "1", "..---": "2",
         "...--": "3", "....-": "4", ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9", "-..-.": "/",
         "..--..": "?", "-...-": "=", ".-.-.": "+", ".-.-.-": ".", "--..--": ","}
UNREAD = "~"                 # a run of marks that is no letter

ENV_RATE = 600.0             # the envelope's samples a second, after the filter
U = 8                        # ...and then per dit, for the search
ZCAP = 5.0                   # a sample can be this many spreads out and no more: strength mustn't drown the timing rules
DIT_MAX, DAH_MAX, GAP1_MAX, GAP3_MAX, IDLE_MIN = 15, 36, 15, 39, 40     # how long each may be, in samples (dit 8, dah 24)
SPEEDS = (10.0, 12.0, 14.5, 17.5, 21.0, 25.0, 30.0, 36.0, 43.0, 51.0)        # words a minute tried when finding the speed
SEARCH_HALF = 22.0           # the filter's half-width while finding the speed, Hz
SQUELCH_Z = 3.8              # the middle mark must stand this many spreads above the noise (noise alone: 2.5 to 3)
MAX_MISFIT = 0.30            # ...and marks and gaps be this near Morse's lengths (mean |log ratio|; a shaky fist: 0.15)
MAX_UNREAD = 0.25            # ...and no more than this share of the runs of marks be no letter at all (a teleprinter: 0.4)

BAND = (250.0, 2800.0)       # where a tone is looked for in the audio, Hz
TONE_OVER_DB = 12.0          # a tone: this far over the rest of the band
QUIET_S = 1.3                # an over has ended when it's been quiet this long
LONG_OVER_S = 6.0            # a longer over is read this far in, at a gap between words
PAD_S = 1.0                  # read with this much either side
KEEP_S = 30.0                # audio kept
DECIMATE = 6                 # of the speaker's rate: 7350 samples a second is plenty for a tone under 2.8 kHz
RATE = pcm.SAMPLE_RATE / DECIMATE
TAPE_S = 8.0                 # of the envelope, for the page's tape
TAPE_RATE = 50.0


def envelope(x: np.ndarray, rate: float, pitch: float, half_width: float) -> tuple[np.ndarray, float]:
    """|x| within pitch +/- half_width Hz, and its samples a second (about ENV_RATE)."""
    z = x * np.exp(-2j * np.pi * pitch / rate * np.arange(len(x)))        # the tone -> 0 Hz
    d1 = max(1, int(rate // (ENV_RATE * 4)))                               # boxcar down to about 2400 Hz
    z = z[:len(z) // d1 * d1].reshape(-1, d1).mean(axis=1)
    r1 = rate / d1
    taps = int(r1 / half_width * 2.5) | 1                                  # a windowed-sinc low pass
    k = np.arange(taps) - taps // 2
    h = np.sinc(2 * half_width / r1 * k) * np.blackman(taps)
    z = np.convolve(z, h / h.sum(), "same")
    d2 = max(1, int(round(r1 / ENV_RATE)))
    return np.abs(z[::d2]), r1 / d2


def refine_pitch(x: np.ndarray, rate: float, pitch: float, reach: float = 35.0) -> float:
    """The strongest tone within pitch +/- reach Hz."""
    n = 1 << int(np.ceil(np.log2(len(x))))
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)), n)) ** 2
    f = np.fft.rfftfreq(n, 1 / rate)
    m = np.flatnonzero((f > pitch - reach) & (f < pitch + reach))
    if not len(m):
        return pitch
    w = max(1, int(3.0 / (rate / n)))                                      # smoothed over about 3 Hz
    return float(f[m][np.argmax(np.convolve(X[m], np.ones(w) / w, "same"))])


def strongest_tone(x: np.ndarray, rate: float) -> float | None:
    """The pitch of the strongest narrow tone that comes and goes in the audio band, or None."""
    n, hop = 2048, 1024
    if len(x) < n * 2:
        return None
    win = np.hanning(n)
    frames = np.array([np.abs(np.fft.rfft(x[a:a + n] * win)) ** 2 for a in range(0, len(x) - n, hop)])
    f = np.fft.rfftfreq(n, 1 / rate)
    band = np.flatnonzero((f >= BAND[0]) & (f <= BAND[1]))
    top = np.percentile(frames[:, band], 90, axis=0)                       # (keyed: its best moments, not its average)
    i = int(np.argmax(top))
    if 10 * np.log10(top[i] / (np.median(frames[:, band]) + 1e-12)) < TONE_OVER_DB:
        return None
    return float(f[band][i])


def _prior(n_max: int, weight: float) -> np.ndarray:
    d = np.arange(n_max + 1, dtype=float)
    d[0] = 1
    return weight * np.minimum(np.log(d / U) ** 2, np.log(d / (3 * U)) ** 2)


def _viterbi(e: np.ndarray, lo: float, hi: np.ndarray, sigma: float, k: float, weights: tuple[float, float]):
    """The best cutting of the envelope e into marks and gaps. -> ([(kind, start, end)], cost less that of
    "nothing was sent"); kind is dit, dah, gap1 (inside a letter), gap3 (between letters) or idle (between
    words, or silence). k: how independent neighbouring samples are (1 = fully)."""
    T = len(e)
    # A strong signal's own level wanders (fading, the key's shape) far more than the noise does: the spread
    # allowed is the noise's or a share of the signal's height, whichever is the larger.
    span = hi - lo
    cmk = k * np.minimum(np.maximum(0.0, hi - e) / np.maximum(sigma, 0.20 * span), ZCAP) ** 2 / 2      # the cost of "key down"
    csp = k * np.minimum(np.maximum(0.0, e - lo) / np.maximum(sigma, 0.12 * span), ZCAP) ** 2 / 2      # ...of "key up"
    cm = np.concatenate(([0.0], np.cumsum(cmk)))
    cs = np.concatenate(([0.0], np.cumsum(csp)))
    pm, pg = _prior(DAH_MAX, weights[0]) + 0.6, _prior(GAP3_MAX, weights[1])
    INF = 1e18
    M = np.full(T + 1, INF)
    G = np.full(T + 1, INF)
    I = np.full(T + 1, INF)                                                # noqa: E741 (mark, gap, idle: ending here)
    I[0] = 0.0
    bM = np.zeros(T + 1, int)
    fM = np.zeros(T + 1, bool)
    bG = np.zeros(T + 1, int)
    bI = np.zeros(T + 1, bool)
    dm_all, dg_all = np.arange(4, DAH_MAX + 1), np.arange(3, GAP3_MAX + 1)
    for t in range(1, T + 1):
        stay = I[t - 1] + csp[t - 1]
        enter = M[t - IDLE_MIN] + cs[t] - cs[t - IDLE_MIN] if t >= IDLE_MIN else INF
        I[t], bI[t] = (enter, True) if enter < stay else (stay, False)
        d = dm_all[:max(0, t - 3)]
        if len(d):
            g, i = G[t - d], I[t - d]
            c = np.minimum(g, i) + cm[t] - cm[t - d] + pm[d]
            j = int(np.argmin(c))
            M[t], bM[t], fM[t] = c[j], d[j], i[j] < g[j]
        d = dg_all[:max(0, t - 2)]
        if len(d):
            c = M[t - d] + cs[t] - cs[t - d] + pg[d]
            j = int(np.argmin(c))
            G[t], bG[t] = c[j], d[j]
    segs, t = [], T
    state = int(np.argmin([M[T], G[T], I[T]]))
    while t > 0:
        if state == 0:
            d = int(bM[t])
            segs.append(("dit" if d <= DIT_MAX else "dah", t - d, t))
            state = 2 if fM[t] else 1
            t -= d
        elif state == 1:
            d = int(bG[t])
            segs.append(("gap1" if d <= GAP1_MAX else "gap3", t - d, t))
            state = 0
            t -= d
        else:
            end = t
            while t > 0 and not bI[t]:
                t -= 1
            if t > 0:
                t -= IDLE_MIN
                state = 0
            segs.append(("idle", t, end))
    return segs[::-1], float(min(M[T], G[T], I[T])) - float(cs[T])


def _signal_level(e: np.ndarray, segs: list, floor: float) -> np.ndarray | None:
    """The signal's level along e, from the marks of a reading (the middle of each, smoothed over seven)."""
    marks = [((a + b) / 2, float(np.median(e[a + 1:b - 1] if b - a > 3 else e[a:b]))) for k, a, b in segs if k in ("dit", "dah")]
    if len(marks) < 4:
        return None
    at, lv = np.array([m[0] for m in marks]), np.array([m[1] for m in marks])
    sm = np.array([np.median(lv[max(0, i - 3):i + 4]) for i in range(len(lv))])
    # (never under a quarter of the over's own level: a strong neighbour's key clicks, heard when this
    # station is silent, are not this station fading)
    return np.maximum(np.interp(np.arange(len(e)), at, sm), max(floor, 0.25 * float(np.percentile(lv, 75))))


def _read(x, rate, pitch, wpm, half, noise, weights, again=1, filtered=None):
    """One reading at one speed. -> (segs, cost, e, lo, sigma, step)"""
    env, er = filtered or envelope(x, rate, pitch, half)
    step = 1.2 / wpm / U
    e = np.interp(np.arange(0, len(env) / er - step, step), np.arange(len(env)) / er, env)
    k = min(1.0, step * 2 * half)
    lo = noise[0] * np.sqrt(half / noise[1])                               # the noise, as this filter lets it through
    sigma = 0.52 * lo                                                      # (a noise envelope's spread is about half its mean)
    win = max(4, int(1.5 / step))
    hop = max(1, win // 4)
    c = np.arange(0, len(e), hop)
    pk = np.array([np.percentile(e[max(0, a - win // 2):a + win // 2 + 1], 93) for a in c])
    hi = np.maximum(np.interp(np.arange(len(e)), c, pk), lo + 2.5 * sigma)
    segs, cost = _viterbi(e, lo, hi, sigma, k, weights)
    for _ in range(again):                                                 # the level from the reading, then read again
        level = _signal_level(e, segs, lo + 2.0 * sigma)
        if level is None:
            break
        segs, cost = _viterbi(e, lo, level, sigma, k, weights)
    return segs, cost, e, lo, sigma, step


def find_speed(x: np.ndarray, rate: float, pitch: float, noise: tuple[float, float]) -> float:
    """Words a minute: the speed whose strict-timing reading beats silence by most (of the first 20 s)."""
    x = x[:int(rate * 20)]
    best = (9e18, 20.0)
    filtered = envelope(x, rate, pitch, SEARCH_HALF)                       # (the same for every speed tried)
    for w in SPEEDS:
        cost = _read(x, rate, pitch, w, SEARCH_HALF, noise, (10.0, 5.0), again=0, filtered=filtered)[1]
        if cost < best[0]:
            best = (cost, w)
    return float(best[1])


def noise_level(x: np.ndarray, rate: float, pitch: float) -> tuple[np.ndarray, float, tuple[float, float]]:
    """-> (the envelope 30 Hz either side of pitch, its samples a second, the noise as (mean, half-width)):
    the noise from the quietest 30% of quarter-seconds."""
    env, er = envelope(x, rate, pitch, 30.0)
    blk = max(1, int(er * 0.25))
    means = env[:len(env) // blk * blk].reshape(-1, blk).mean(axis=1)
    quiet = np.sort(means)[:max(2, len(means) * 3 // 10)] if len(means) else env
    return env, er, (max(float(np.median(quiet)), 1e-9), 30.0)


def overs(env: np.ndarray, er: float, noise: tuple[float, float], quiet_s: float = QUIET_S) -> list[tuple[float, float]]:
    """The stretches where something is keying. -> [(start_s, end_s)]"""
    lo = noise[0]
    w = max(1, int(er * 0.06))
    on = np.flatnonzero(np.convolve(env, np.ones(w) / w, "same") > lo + 3.5 * 0.52 * lo)
    if not len(on):
        return []
    out, a, last = [], on[0], on[0]
    for i in on[1:]:
        if i - last > quiet_s * er:
            out.append((a, last))
            a = i
        last = i
    out.append((a, last))
    return [(a / er, b / er) for a, b in out if (b - a) / er > 0.25]


def decode(x: np.ndarray, rate: float, pitch: float, noise: tuple[float, float], wpm: float | None = None) -> dict:
    """Read one over. -> {"text", "wpm", "pitch", "quality" (0-1), "chars": [(char, time_s, confidence)],
    "marks": [(start_s, end_s)]}; text "" if there's nothing that stands up as Morse."""
    pitch = refine_pitch(x, rate, pitch)
    found = wpm or find_speed(x, rate, pitch, noise)
    out = {"text": "", "wpm": found, "pitch": pitch, "quality": 0.0, "chars": [], "marks": []}
    w = found
    for _ in range(2):                                                     # the speed, again from what was read
        segs, _, e, lo, sigma, step = _read(x, rate, pitch, w, max(8.0, 0.8 * w / 1.2), noise, (1.5, 0.8))
        marks = [(k, b - a) for k, a, b in segs if k in ("dit", "dah")]
        if len(marks) < 2:
            return out
        # the dit as sent, in samples of the one read for: from marks and gaps alike (the filter stretches a
        # mark by what it takes off the gap beside it)
        sizes = [(b - a) / (1.0 if k in ("dit", "gap1") else 3.0) for k, a, b in segs[1:-1] if k != "idle"]
        unit = float(np.mean(sizes)) / U if sizes else 1.0
        if abs(unit - 1) < 0.06:
            break
        w, unit = float(np.clip(w / unit, found * 0.75, found * 1.33)), 1.0
    chars, sym, start, conf, zs, misfit = [], "", 0, [], [], []

    def flush(space_at: int | None) -> None:
        nonlocal sym, conf
        if sym:
            ch = MORSE.get(sym)
            chars.append((ch or UNREAD, start * step, float(np.clip((min(conf) - 2.5) / 4.0, 0, 1)) if ch else 0.0))
        if space_at is not None and chars and chars[-1][0] != " ":
            chars.append((" ", space_at * step, 1.0))
        sym, conf = "", []

    for k, a, b in segs:
        if k in ("dit", "dah"):
            if not sym:
                start = a
            sym += "." if k == "dit" else "-"
            z = float((np.mean(e[a:b]) - lo) / sigma)                      # how far above the noise this mark stood
            zs.append(z)
            conf.append(z)
            misfit.append(abs(np.log((b - a) / (U if k == "dit" else 3 * U))))
            if len(sym) > 7:
                flush(None)
        elif k == "gap1":
            misfit.append(abs(np.log((b - a) / U)))
        elif k == "gap3":
            misfit.append(abs(np.log((b - a) / (3 * U))))
            flush(None)
        else:
            flush(a)
    flush(None)
    while chars and chars[-1][0] == " ":
        chars.pop()
    letters = [c for c in chars if c[0] != " "]
    out.update(wpm=w / unit, z=float(np.median(zs)), misfit=float(np.mean(misfit)),
               quality=float(np.mean([c[2] for c in letters])) if letters else 0.0)
    unread = sum(1 for c in letters if c[0] == UNREAD)
    if out["z"] < SQUELCH_Z or out["misfit"] > MAX_MISFIT or (unread > 1 and unread >= MAX_UNREAD * len(letters)):     # nothing rather than rubbish
        return out
    out.update(text="".join(c[0] for c in chars), chars=chars,
               marks=[(a * step, b * step) for k, a, b in segs if k in ("dit", "dah")])
    return out


class Reader:
    """Listens to what a receiver plays and reads the Morse in it, an over at a time, on its own thread."""

    def __init__(self) -> None:
        self.on = False
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] = []
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._x = np.zeros(0, np.float32)            # the audio kept, at RATE
        self._x0 = 0                                 # ...and where it starts, in samples since switched on
        self._done = 0                               # read up to here
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._n = 0
        self._overs: deque[dict] = deque(maxlen=40)
        self._tape: dict = {"at": 0.0, "pitch": None, "wpm": None, "env": []}
        self._floor: float | None = None             # the noise, remembered: an over with no quiet in it is read against this
        self._going: tuple[float, float] | None = None   # (pitch, words a minute) of a long over part-read
        self._tone: tuple[int, float | None] = (-10**9, None)   # (when looked for, the pitch found)

    # --- the switch, and the sound coming in -------------------------------------------------

    def switch(self, on: bool) -> None:
        on = bool(on)
        with self._lock:
            if on == self.on:
                return
            self.on = on
            self._pending, self._left = [], np.zeros(0, np.float32)
            if on:
                self._x, self._x0, self._done, self._n = np.zeros(0, np.float32), 0, 0, 0
                self._overs.clear()
                self._floor, self._going, self._tone = None, None, (-10**9, None)
                self._tape = {"at": 0.0, "pitch": None, "wpm": None, "env": []}
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._run, args=(self._stop,), name="morse-reader", daemon=True)
                self._thread.start()
            else:
                self._stop.set()

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

    # --- what the page asks for --------------------------------------------------------------

    def status(self, since: int = 0) -> dict:
        """{"on", "at" (s of audio heard), "pitch", "wpm", "env" (the tape: 0-255 at TAPE_RATE, ending at "at"),
        "overs": [{"n", "t0", "t1", "pitch", "wpm", "quality", "chars": [[char, at, confidence]], "marks": [[from, to]]}]}"""
        with self._lock:
            return {"on": self.on, **self._tape, "tape_rate": TAPE_RATE, "overs": [o for o in self._overs if o["n"] > since]}

    # --- the reading -------------------------------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(0.25):
            try:
                self.step()
            except Exception:                        # (a reader that falls over mustn't take anything with it)
                log.exception("morse: reading failed")
                time.sleep(1.0)

    def step(self) -> None:
        """Take in the sound that's arrived, and read whatever over has ended in it."""
        with self._lock:
            new, self._pending = self._pending, []
        if new:
            self._x = np.concatenate([self._x, *new])
            over = len(self._x) - int(KEEP_S * RATE)
            if over > 0:
                self._x, self._x0 = self._x[over:], self._x0 + over
        end = self._x0 + len(self._x)
        a = max(self._x0, self._done - int(PAD_S * RATE))
        x = self._x[a - self._x0:]
        if len(x) < RATE * 1.5:
            return
        if end - self._tone[0] >= RATE:              # (looked for once a second)
            self._tone = (end, strongest_tone(x[-int(RATE * 8):], RATE))
        pitch = self._going[0] if self._going else self._tone[1]
        if pitch is None:
            self._done = max(self._done, end - int(RATE * (QUIET_S + PAD_S)))
            self._show(end, None, None, None, 0.0)
            return
        env, er, noise = noise_level(x, RATE, pitch)
        # (a stretch that is all sending has no quiet to measure: the least seen lately stands, creeping up
        # so that a noisier band is believed within a minute)
        self._floor = noise[0] if self._floor is None else min(noise[0], self._floor * 1.005)
        noise = (self._floor, noise[1])
        self._show(end, pitch, env[-int(er * TAPE_S):], er, noise[0])
        spans, now = overs(env, er, noise), len(x) / RATE
        for s0, s1 in spans:
            i0 = a + int(s0 * RATE)
            if a + int(s1 * RATE) <= self._done:
                continue
            ended = now - s1 > QUIET_S
            if not ended and s1 - max(s0, (self._done - a) / RATE) < LONG_OVER_S:
                break                                # still being sent: wait for it
            carried = self._going is not None and i0 < self._done     # the rest of a long over: from the gap it was cut at, at its speed
            # (otherwise from where it began, even if that was while another station, at another pitch, was being read)
            i0 = max(i0, self._done if carried else self._done - int(3 * RATE))
            lead = 0 if carried else min(i0 - a, int(PAD_S * RATE))      # (quiet before it, to read it against)
            j1 = min(len(x), int((s1 + PAD_S) * RATE)) if ended else len(x)
            seg = x[i0 - a - lead:j1]
            r = decode(seg, RATE, pitch, noise, wpm=self._going[1] if carried else None)
            t0 = (i0 - lead) / RATE                  # the segment's start, in seconds since switched on
            chars, marks = r["chars"], r["marks"]
            if ended:
                self._done, self._going = a + int(s1 * RATE) + int(0.2 * RATE), None
            else:                                    # a long over: as far as the last gap between words, the rest next time
                gaps = [i for i, c in enumerate(chars) if c[0] == " "]
                if gaps:
                    g = gaps[-1]
                    cut = (chars[g][1] + chars[g + 1][1]) / 2          # the middle of that gap
                    chars, marks = chars[:g], [m for m in marks if m[1] <= cut]
                else:
                    cut = marks[-1][1] + 0.05 if marks else len(seg) / RATE - 0.5
                self._done = i0 - lead + int(cut * RATE)
                self._going = (r["pitch"], r["wpm"]) if r["text"] else None
            if chars:
                self._n += 1
                over = {"n": self._n, "t0": round(t0 + (marks[0][0] if marks else 0), 3), "t1": round(t0 + (marks[-1][1] if marks else 0), 3),
                        "pitch": round(r["pitch"]), "wpm": round(r["wpm"], 1), "quality": round(r["quality"], 2),
                        "chars": [[c, round(t0 + at, 3), round(conf, 2)] for c, at, conf in chars],
                        "marks": [[round(t0 + m0, 3), round(t0 + m1, 3)] for m0, m1 in marks]}
                with self._lock:
                    self._overs.append(over)
                    self._tape["wpm"] = over["wpm"]
            if not ended:
                break

    def _show(self, end: int, pitch: float | None, env: np.ndarray | None, er: float | None, lo: float) -> None:
        tape: list[int] = []
        if env is not None and len(env):
            top = max(float(np.percentile(env, 99)), lo * 6)               # (noise alone stays low on the tape)
            n = int(len(env) / er * TAPE_RATE)
            tape = np.clip((np.interp(np.arange(n) / TAPE_RATE, np.arange(len(env)) / er, env) - lo) / (top - lo) * 255, 0, 255).astype(int).tolist()
        with self._lock:
            self._tape = {"at": round(end / RATE, 3), "pitch": None if pitch is None else round(pitch), "wpm": self._tape.get("wpm"), "env": tape}


reader = Reader()
