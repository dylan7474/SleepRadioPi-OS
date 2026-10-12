"""RTTY (radio teleprinter) read off a receiver's audio, as text.

RTTY is the oldest keyboard mode still in daily use: two tones, one for
"mark" and one for "space", switched 45.45 times a second on the amateur
bands (170 Hz apart; contest weekends fill 14.080 to 14.100 MHz with them)
and 50 times a second by the weather and shipping broadcasts (450 Hz apart:
the German weather service on 10.1008 MHz runs all day). Each character is
five bits between a start bit (space) and a stop bit (mark, half as long
again): the 1930s teleprinter code, capitals only, with a shift to figures
and back.

How a signal is read:

  * Found by its shape: two humps in the spectrum of the last few seconds,
    170, 450 or 850 Hz apart and about as strong as each other (find()).
  * Each tone brought to 0 Hz and filtered to the keying's width; which is
    the stronger, moment by moment, is mark or space. Each is measured
    against its own strength, so a tone that fades more than the other (it
    happens: they are different frequencies) doesn't lose its bits.
  * Framed as a teleprinter does: a start bit found, the five bits after it
    looked at in their middles, the stop bit checked.
  * Which tone is mark depends on the sideband it's heard in, and the speed
    on who is sending: both are tried, and the one whose stop bits are where
    they should be is the reading (read()).

Nothing rather than rubbish: noise frames wrongly about half the time, a
signal almost never. A stretch is only said when nearly all its characters
have their stop bits (FRAMED_MIN). And a teleprinter sends one tone or the
other, never both: two steady tones the right distance apart (carriers, the
edges of some other mode) with noise deciding between them frame now and
then, and did, as "KKHXK8'QQCKBU" on 14.090 MHz. So the weaker of the two
tones, moment by moment, must be small beside the stronger (BOTH_MAX: 0.1
on the German weather service off the air, 0.27 on a signal 5 dB under the
noise, 0.53 for noise alone, 0.67 for those two carriers).

read() does a stretch of sound; the reader that follows a signal over time
is audio/keyboard.py.
"""
from __future__ import annotations

import numpy as np

from sleepradiopi.audio.psk import _low_pass, spectrum, to_base

BAND = (300.0, 3000.0)       # where the tones are looked for in the audio, Hz
SHIFTS = (170.0, 450.0, 850.0)       # how far apart the two tones may be
BAUDS = (45.45, 50.0, 75.0)
OVER_DB = 6.0                # both tones: this far over the noise
BASE_RATE = 1225.0           # samples a second once a tone is at 0 Hz (27 a bit at 45 baud, 16 at 75)
FRAMED_MIN = 0.92            # of the characters framed, this share must have their stop bit (noise: about a half)
CHARS_MIN = 6                # ...and there must be this many
NEAR_S = 1.5                 # a character is said if FRAMED_NEAR of those in this long before it, or after it, are
FRAMED_NEAR = 0.85           # framed: a signal that stops is followed by nothing, not by noise read as letters
BOTH_MAX = 0.42              # the weaker tone of the two, on average, against the stronger: over this it isn't one tone at a time
LEVEL_MIN = 0.3              # ...and it's as strong as this much of what the signal is (about 10 dB down): next door's keying
                             # leaks into a signal's tones once it has stopped, and frames, being a teleprinter's too

NUL, LF, CR, FIGS, LTRS = "\0", "\n", "\r", "\x0e", "\x0f"
LETTERS = [NUL, "E", LF, "A", " ", "S", "I", "U", CR, "D", "R", "J", "N", "F", "C", "K",
           "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", FIGS, "M", "X", "V", LTRS]
# (the figures as American machines and amateur programs have them -- $ ! & # ' " and the bell, which says nothing
# here -- but for = , which the weather broadcasts end every report with: American machines have ; there)
FIGURES = [NUL, "3", LF, "-", " ", "", "8", "7", CR, "$", "4", "'", ",", "!", ":", "(",
           "5", '"', ")", "2", "#", "6", "0", "1", "9", "?", "&", FIGS, ".", "/", "=", LTRS]


def send(text: str, rate: float, low: float, shift: float = 170.0, baud: float = 45.45, mark_high: bool = True, lead: float = 0.5) -> np.ndarray:
    """text as RTTY (amplitude 1), the tones at low and low + shift, with `lead` seconds of mark either side. For the tests."""
    bits, figs = [], False
    for c in text.upper():
        table = FIGURES if figs else LETTERS
        if c not in table or c == "":
            other = LETTERS if figs else FIGURES
            if c not in other:
                continue
            code = FIGS if not figs else LTRS
            figs = not figs
            bits += [0] + [(table.index(code) >> i) & 1 for i in range(5)] + [1, 1, 1]
            table = other
        bits += [0] + [(table.index(c) >> i) & 1 for i in range(5)] + [1, 1, 1]       # (halves: a start, five bits, a stop and a half)
    # in half bits, so that the stop bit can be one and a half
    halves = []
    for i in range(0, len(bits), 9):
        ch = bits[i:i + 9]
        halves += [b for b in ch[:6] for _ in (0, 1)] + [1, 1, 1]
    n_lead = int(lead * rate)
    spb = rate / baud / 2
    n = int(len(halves) * spb)
    k = np.minimum((np.arange(n) / spb).astype(int), len(halves) - 1)
    mark = np.concatenate((np.ones(n_lead, bool), np.array(halves, bool)[k], np.ones(n_lead, bool)))
    f = np.where(mark == mark_high, low + shift, low)
    return np.cos(2 * np.pi * np.cumsum(f) / rate)


def find(x: np.ndarray, rate: float) -> tuple[float, float, float] | None:
    """Where there may be an RTTY signal in x -> (the lower tone Hz, how far apart Hz, dB over the noise: the
    weaker tone's), the strongest pair; None if there isn't one."""
    f, p = spectrum(x, rate)
    step = f[1]
    w = max(3, int(40.0 / step) | 1)
    wide = np.convolve(p, np.ones(w) / w, "same")
    band = (f >= BAND[0]) & (f <= BAND[1])
    floor = float(np.median(wide[band]))
    if floor <= 0:
        return None
    best = None
    for shift in SHIFTS:
        k = int(round(shift / step))
        idx = np.flatnonzero(band & (f + shift <= BAND[1]))
        if not len(idx):
            continue
        # two humps and a dip between them (one wide signal, or a carrier, is not a teleprinter)
        both = np.minimum(wide[idx], wide[idx + k])
        dip = wide[idx + k // 2]
        both = np.where(dip < 0.5 * both, both, 0.0)
        i = int(np.argmax(both))
        db = 10 * np.log10(max(both[i], 1e-30) / floor)
        if db >= OVER_DB and (best is None or db > best[2] + 1.0):       # (a wider pair has to be clearly the better)
            lo = _centre(f, p, idx[i], w)
            hi = _centre(f, p, idx[i] + k, w)
            best = (lo, hi - lo, float(db))
    if best is None:
        return None
    lo, apart, db = best
    shift = min(SHIFTS, key=lambda s: abs(s - apart))
    mid = lo + apart / 2
    return mid - shift / 2, shift, db


TONE_CLEAR_HZ = 60.0         # two signals' tones are at least this far apart


def find_all(x: np.ndarray, rate: float, most: int = 4) -> list[tuple[float, float, float]]:
    """Every RTTY signal there may be in x -> [(the lower tone Hz, how far apart Hz, dB over the noise: the weaker
    tone's)], the strongest first: pairs of humps as find() sees them, none sharing a tone with a stronger one.
    (In a contest there are a dozen side by side; a pair made of two neighbours' tones doesn't frame, and is
    dropped by whoever reads it.)"""
    f, p = spectrum(x, rate)
    step = f[1]
    w = max(3, int(40.0 / step) | 1)
    wide = np.convolve(p, np.ones(w) / w, "same")
    band = (f >= BAND[0]) & (f <= BAND[1])
    floor = float(np.median(wide[band]))
    if floor <= 0:
        return []
    clear = max(1, int(TONE_CLEAR_HZ / step))
    pairs = []
    for n, shift in enumerate(SHIFTS):
        k = int(round(shift / step))
        idx = np.flatnonzero(band & (f + shift <= BAND[1]))
        if not len(idx):
            continue
        both = np.minimum(wide[idx], wide[idx + k])
        both = np.where(wide[idx + k // 2] < 0.5 * both, both, 0.0)       # (a dip between them, as in find())
        for _ in range(most * 2):                    # the peaks of it, the tallest first
            i = int(np.argmax(both))
            db = 10 * np.log10(max(both[i], 1e-30) / floor)
            if db < OVER_DB:
                break
            lo, hi = _centre(f, p, idx[i], w), _centre(f, p, idx[i] + k, w)
            mid = (lo + hi) / 2
            pairs.append((db - n, mid - shift / 2, shift, float(db)))     # (a wider pair has to be clearly the better)
            both[max(0, i - clear):i + clear + 1] = 0.0
    found: list[tuple[float, float, float]] = []
    for _, low, shift, db in sorted(pairs, reverse=True):
        mine = (low, low + shift)
        if any(abs(a - b) < TONE_CLEAR_HZ for g, s, _ in found for a in mine for b in (g, g + s)):
            continue
        found.append((low, shift, db))
        if len(found) >= most:
            break
    return found


def _centre(f: np.ndarray, p: np.ndarray, i: int, w: int) -> float:
    a, b = max(0, i - w // 2), min(len(p), i + w // 2 + 1)
    return float(np.sum(f[a:b] * p[a:b]) / np.sum(p[a:b]))


def tones(x: np.ndarray, rate: float, low: float, shift: float) -> tuple[list[np.ndarray], float]:
    """The two tones, each brought to 0 Hz -> ([the lower, the upper], their samples a second)."""
    both = [to_base(x, rate, tone, BASE_RATE) for tone in (low, low + shift)]
    return [z for z, _ in both], both[0][1]


def keying(base: list[np.ndarray], r: float, baud: float) -> tuple[np.ndarray, list[np.ndarray]]:
    """Which tone is there, moment by moment, from tones() -> (+ for the upper, - for the lower, about 1 either
    way; how strong each of the two is, moment by moment: [the lower, the upper]). The line between them is drawn half way between
    the two tones' own strengths of late, so a tone that has faded more than the other still has its bits, and
    a tone not sent at all (the idle one) reads as the other."""
    e, top = [], []
    block = max(1, int(r * 0.1))
    for z in base:
        a = np.abs(_low_pass(z, r, 0.75 * baud, int(r / baud * 3) | 1))
        # its strength of late: what it usually reaches in a tenth of a second, over the two seconds around (the
        # most it reached drew the line too high on a real broadcast: a burst of noise set it)
        n = len(a) // block
        if n < 2:
            peak = np.full(len(a), a.max() if len(a) else 0.0)
        else:
            m = a[:n * block].reshape(n, block).max(axis=1)
            m = np.median(np.stack([np.roll(np.pad(m, 10, mode="edge"), s)[10:-10] for s in range(-10, 11)]), axis=0)
            peak = np.interp(np.arange(len(a)), (np.arange(n) + 0.5) * block, m)
        e.append(a)
        top.append(peak)
    scale = (top[0] + top[1]) / 2 + 1e-12
    return ((e[1] - e[0]) - (top[1] - top[0]) / 2) / scale, e


def frame(k: np.ndarray, r: float, baud: float, start: int = 0) -> tuple[list[tuple[int, int, float]], list[int]]:
    """Teleprinter characters in the keying k (+ is mark), from sample `start` -> ([(the five bits as a number, the
    sample its stop bit ends at, how sure 0-1)], [where a start bit had no stop bit after it])."""
    spb = r / baud
    mark = k > 0
    edges = np.flatnonzero(mark[:-1] & ~mark[1:]) + 1          # mark -> space: a start bit may begin here
    chars, bad, i = [], [], start
    last = len(k) - int(7.5 * spb) - 1
    j = int(np.searchsorted(edges, i))
    while j < len(edges):
        e = int(edges[j])
        if e > last:
            break
        if e < i:
            j += 1
            continue
        mids = (e + (np.arange(7) + 0.5) * spb).astype(int)     # the middles of the start, the five bits, the stop
        span = max(1, int(spb * 0.25))
        v = np.array([k[m - span:m + span + 1].mean() for m in mids])
        if v[0] > 0:                                 # (not a start bit after all: a blip)
            j += 1
            continue
        if v[6] <= 0:                                # no stop bit: not framed here
            bad.append(e)
            j += 1
            continue
        code = sum(1 << b for b in range(5) if v[1 + b] > 0)
        i = int(e + 7.0 * spb)                       # (the stop bit's a bit and a half: the next start can come from its middle)
        chars.append((code, i, float(min(1.0, np.abs(v).min() * 2))))
        j = int(np.searchsorted(edges, int(e + 6.5 * spb)))
    return chars, bad


def read(x: np.ndarray, rate: float, low: float, shift: float, baud: float | None = None, mark_high: bool | None = None,
         level: float | None = None) -> dict:
    """The RTTY in x with its tones at low and low + shift -> {"baud", "mark_high", "framed" (the share of characters
    with their stop bit, over the stretch that's said), "level" (how strong it is where it's said), "both" (the
    weaker tone against the stronger there: small for a teleprinter, see the top), "codes": [(five
    bits, when it ended: s into x, how sure)]}: the speed and the sense that say most, of those tried (all of
    them, unless given). Only the characters with good framing around them are said, and only those about as
    strong as the signal is (`level`, if the caller has been following it; else as the rest of what's said here); when the speed and sense are being found, none at all unless there are
    enough of them and the stretch they make is nearly all framed. (The stretch, not all of x: a signal that
    has only just begun has seconds of noise before it, which frames badly at any speed -- judged over all of
    it, a station's first words were lost while its framing "improved".)"""
    best = {"baud": baud, "mark_high": mark_high, "framed": 0.0, "level": 0.0, "both": 1.0, "codes": []}
    base, r = tones(x, rate, low, shift)
    near = NEAR_S * r
    for b in ((baud,) if baud else BAUDS):
        k, each = keying(base, r, b)
        strength = each[0] + each[1]
        for high in ((mark_high,) if mark_high is not None else (True, False)):
            chars, bad = frame(k if high else -k, r, b)
            if not chars:
                continue
            ends, bad = np.array([at for _, at, _ in chars]), np.array(bad)

            def side(lo: float, hi: float) -> bool:      # well framed between these?
                good = int(np.sum((ends >= lo) & (ends <= hi)))
                return good >= 4 and good / (good + int(np.sum((bad >= lo) & (bad <= hi)))) >= FRAMED_NEAR
            # (before it or after it: the first and last of an over have noise on one side)
            said = [(code, at, sure) for code, at, sure in chars if side(at - near, at) or side(at, at + near)]
            was = level
            if said:                                 # ...and as strong as the signal: its own level where it's said, unless told
                loud = np.array([strength[max(0, int(at - 4.0 * r / b))] for _, at, _ in said])      # (in the middle of each)
                was = float(np.median(loud)) if level is None else level
                said = [c for c, l in zip(said, loud) if l >= LEVEL_MIN * was]
            both = 1.0
            if said:
                lo, hi = said[0][1], said[-1][1]
                n = int(np.sum((ends >= lo) & (ends <= hi)))
                framed = n / (n + int(np.sum((bad >= lo) & (bad <= hi))))
                a, z = max(0, int(lo - 7.5 * r / b)), int(hi) + 1          # (one tone at a time, over the stretch that's said)
                both = float(np.minimum(each[0][a:z], each[1][a:z]).mean() / (np.maximum(each[0][a:z], each[1][a:z]).mean() + 1e-12))
                if both > BOTH_MAX:
                    said = []
            else:
                framed = len(chars) / (len(chars) + len(bad))
            score = (len(said) * b, framed)          # (most characters well framed: the wrong speed frames fewer, worse)
            if score > best.get("score", (0.0, 0.0)) or "score" not in best:
                best = {"baud": b, "mark_high": high, "framed": framed, "score": score, "level": was if said else 0.0, "both": both,
                        "codes": [(code, at / r, sure) for code, at, sure in said]}
    best.pop("score", None)
    finding = baud is None or mark_high is None
    if finding and (len(best["codes"]) < CHARS_MIN or best["framed"] < FRAMED_MIN):
        best["codes"] = []
    return best


def text(codes: list[tuple[int, float, float]], figs: bool = False) -> tuple[list[tuple[str, float, float]], bool]:
    """Five-bit codes -> ([(char, when, how sure)], whether it's left in figures): letters and figures by the shifts."""
    out = []
    for code, when, sure in codes:
        c = (FIGURES if figs else LETTERS)[code]
        if c == FIGS:
            figs = True
        elif c == LTRS:
            figs = False
        elif c and c != NUL:
            out.append((c, when, sure))
    return out, figs
