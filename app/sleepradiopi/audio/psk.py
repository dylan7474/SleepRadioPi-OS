"""PSK31 and PSK63 read off a receiver's audio, as text.

PSK31 is the amateur bands' keyboard-to-keyboard mode: a steady tone whose
phase is turned over, or not, 31.25 times a second -- a turn is a 0, none is
a 1 -- with the tone's strength dipped to nothing at each turn, so the
whole signal is only about 31 Hz wide. A dozen of them fit side by side in
the 2.5 kHz a sideband receiver hears, around 14.070 MHz and its like on the
other bands, and they are all read at once here.

How a signal is read:

  * Found by its shape: a hump about 60 Hz wide standing out of the noise in
    the spectrum of the last few seconds (find()).
  * Brought to 0 Hz and filtered to its own width, so the signal 100 Hz away
    makes no difference.
  * The moment in each 32 ms to look at is where the strength is greatest
    (the turns are where it dips): found by folding the strength on the
    symbol rate.
  * A turn or not is the phase now against the phase one symbol ago, so the
    tone's exact pitch doesn't matter much; what's left of a pitch error is
    measured (the same comparison squared loses the turns) and taken out.
  * Letters are Varicode: each a run of bits with no two 0s together, with
    "00" between letters, the common letters short (e is "11", a space "1").

Nothing rather than rubbish: noise makes turns at random, which read as
strings of odd letters. A signal's turns are clean -- how nearly each
comparison lands on "turned" or "not" (the eye, 1.0 when perfect, about
0.64 for noise) is what decides whether a letter is said at all, second by
second: a station that stops is followed by nothing, not by noise read as
letters. And a plain steady tone, which has a perfect eye and never turns,
is left alone (on the air it read as "e  t e", from the odd turn noise
gave it) -- as is one 16 Hz off pitch, which looks like a turn every symbol
but never dips.

PSK63 is the same thing at twice the speed: 62.5 turns a second, twice as
wide, used where there's signal to spare (contests, mostly). Everything
here takes the speed (`baud`) and does the same at either. A signal doesn't
say which it is, so the reader that follows them tries a new one at each
speed: read at the wrong one, PSK31 never makes the two 0s that end a
letter and PSK63 is filtered away, so nothing comes of it.

QPSK31 is the four-phase kind, seldom heard: the same speed and width, but
each symbol turns the phase by a quarter turn either way, a half, or not at
all, and carries two bits -- not two bits of text, but the two outputs of
an error-correcting code (rate 1/2, five bits deep: the polynomials 0x17
and 0x19, as G3PLX's PSK31 has them and fldigi sends them) fed with the
same Varicode bits. A symbol damaged by noise is put right from those
around it (qpsk_bits(): a Viterbi decoder, over the whole stretch at once).
What an error-correcting code gives for free is a test of whether it is
the mode at all: the text read, coded again, must give back the symbols
heard. Where it does, nearly symbol for symbol, the letters are said; a
plain PSK31 signal, noise or a whistle doesn't. Which way the quarter turns
go depends on the sideband: read_qpsk(reverse=True) is the other.

read() does one signal in a stretch of sound; the reader that follows a
band of them over time is audio/keyboard.py.
"""
from __future__ import annotations

import numpy as np

BAUD = 31.25
BAUDS = (31.25, 62.5)        # PSK31 and PSK63
NAMES = {31.25: 31, 62.5: 63}        # ...as they're called
BAND = (300.0, 2900.0)       # where signals are looked for in the audio, Hz
OVER_DB = 5.0                # a signal: this far over the noise, in a 60 Hz width
APART = 60.0                 # two signals are at least this far apart, Hz
BASE_RATE = 500.0            # samples a second once a PSK31 signal is at 0 Hz (16 a symbol; twice that for PSK63)
EYE_MIN = 0.80               # the eye a stretch must have to be said (noise: 0.64; a clean signal: over 0.95)
TURNS_MIN = 0.85             # a tone that hardly ever turns is a carrier (a whistle), not PSK31: text turns for a third of its bits
DIP_MAX = 0.55               # ...and at a turn the strength must dip (to nothing, on a clean signal): under this much of itself
EYE_OVER = 33                # ...taken over this many symbols, about a second of PSK31 (noise alone then strays to 0.8 very seldom)
LETTERS_MIN = 6              # heard(): a signal has to say this many letters...
SURE_MIN = 0.8               # ...each as sure as this, on average (PSK31: 0.99; Morse read as PSK31 says "letters" too, at 0.54)

QPSK_AGREE_MIN = 0.85        # QPSK: of the symbols about a letter, this share must be what the text read codes back to (noise: about a third)
QPSK_FOURTH_MIN = 0.35       # ...and worth trying at all only if the phase steps keep to four places (their fourth power agrees with itself)
# what the code puts out for each five bits (the newest lowest): bit 0 from 0x17, bit 1 from 0x19
_QPSK_OUT = np.array([(bin(0x17 & i).count("1") & 1) | ((bin(0x19 & i).count("1") & 1) << 1) for i in range(32)])
_QPSK_TURN = np.array([-1, 1j, 1, -1j])      # the turn of phase each of the four makes, heard in upper sideband (0: a half turn, as PSK31's idle)

# Varicode (G3PLX): the bits of each character, by its ASCII number
_VARICODE = """1010101011 1011011011 1011101101 1101110111 1011101011 1101011111 1011101111 1011111101
1011111111 11101111 11101 1101101111 1011011101 11111 1101110101 1110101011
1011110111 1011110101 1110101101 1110101111 1101011011 1101101011 1101101101 1101010111
1101111011 1101111101 1110110111 1101010101 1101011101 1110111011 1011111011 1101111111
1 111111111 101011111 111110101 111011011 1011010101 1010111011 101111111
11111011 11110111 101101111 111011111 1110101 110101 1010111 110101111
10110111 10111101 11101101 11111111 101110111 101011011 101101011 110101101
110101011 110110111 11110101 110111101 111101101 1010101 111010111 1010101111
1010111101 1111101 11101011 10101101 10110101 1110111 11011011 11111101
101010101 1111111 111111101 101111101 11010111 10111011 11011101 10101011
11010101 111011101 10101111 1101111 1101101 101010111 110110101 101011101
101110101 101111011 1010101101 111110111 111101111 111111011 1010111111 101101101
1011011111 1011 1011111 101111 101101 11 111101 1011011
101011 1101 111101011 10111111 11011 111011 1111 111
111111 110111111 10101 10111 101 110111 1111011 1101011
11011111 1011101 111010101 1010110111 110111011 1010110101 1011010111 1110110101""".split()
VARICODE = {bits: chr(i) for i, bits in enumerate(_VARICODE)}
ENCODE = {c: bits for bits, c in VARICODE.items()}
PRINTS = frozenset(chr(i) for i in range(32, 127)) | {"\n", "\r"}


def send(text: str, rate: float, pitch: float, lead: int = 24, baud: float = BAUD) -> np.ndarray:
    """text as PSK31 (or PSK63: baud) at this pitch (amplitude 1), with `lead` idle symbols either side. For the tests."""
    bits = [0] * lead + [int(b) for c in text for b in ENCODE[c] + "00"] + [0] * lead
    sps = rate / baud
    n = int(len(bits) * sps)
    t = np.arange(n) / sps                           # in symbols
    k = np.minimum(t.astype(int), len(bits) - 1)
    sign = np.cumprod([1] + [1 if b else -1 for b in bits])       # the phase in each symbol (a 0 turns it)
    a, b = sign[k], sign[k + 1]                      # ...and the one it's on its way to, by the symbol's end
    frac = t - k
    shape = np.where(a == b, a, a * np.cos(np.pi * frac))        # a turn: down to nothing and up the other way
    return shape * np.cos(2 * np.pi * pitch * np.arange(n) / rate)


def _low_pass(z: np.ndarray, rate: float, cutoff: float, taps: int) -> np.ndarray:
    k = np.arange(taps) - taps // 2
    h = np.sinc(2 * cutoff / rate * k) * np.blackman(taps)
    return np.convolve(z, h / h.sum(), "same")


def to_base(x: np.ndarray, rate: float, pitch: float, about: float) -> tuple[np.ndarray, float]:
    """x with pitch brought to 0 Hz, at about `about` samples a second -> (complex samples, their rate). In two
    stages, a plain average and then a filter, so the filter has a fraction of the samples to do (as the
    Morse reader does it: this is most of what reading costs)."""
    z = x * np.exp(-2j * np.pi * pitch / rate * np.arange(len(x)))
    d1 = max(1, int(rate // (about * 3)))
    z = z[:len(z) // d1 * d1].reshape(-1, d1).mean(axis=1)
    r1 = rate / d1
    d2 = max(1, int(round(r1 / about)))
    if d2 > 1:
        z = _low_pass(z, r1, 0.4 * r1 / d2, 8 * d2 + 1)[::d2]
    return z, r1 / d2


def spectrum(x: np.ndarray, rate: float, n: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """The power in x by frequency, averaged over it -> (Hz, power)."""
    hop = n // 2
    if len(x) < n:
        x = np.concatenate((x, np.zeros(n - len(x))))
    w = np.hanning(n)
    frames = [x[i:i + n] * w for i in range(0, len(x) - n + 1, hop)]
    return np.fft.rfftfreq(n, 1 / rate), np.mean(np.abs(np.fft.rfft(frames, axis=1)) ** 2, axis=0)


def find(x: np.ndarray, rate: float, most: int = 8) -> list[tuple[float, float]]:
    """Where there may be PSK31 signals in x -> [(pitch Hz, dB over the noise)], the strongest first."""
    f, p = spectrum(x, rate)
    step = f[1]
    w = max(3, int(APART / step) | 1)
    wide = np.convolve(p, np.ones(w) / w, "same")
    band = (f >= BAND[0]) & (f <= BAND[1])
    floor = float(np.median(wide[band]))
    if floor <= 0:
        return []
    found = []
    idx = np.flatnonzero(band)
    order = idx[np.argsort(wide[idx])[::-1]]
    for i in order:
        db = 10 * np.log10(wide[i] / floor)
        if db < OVER_DB or len(found) >= most:
            break
        if any(abs(f[i] - g) < APART for g, _ in found):
            continue
        a, b = max(0, i - w // 2), min(len(p), i + w // 2 + 1)
        found.append((float(np.sum(f[a:b] * p[a:b]) / np.sum(p[a:b])), float(db)))       # (the middle of its power)
    return found


def _symbols(x: np.ndarray, rate: float, pitch: float, baud: float = BAUD) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The signal at pitch, once a symbol at the best moment -> (the symbols, complex; when each is, s into x;
    the strength half way from each to the one before, where a turn dips to nothing)."""
    z, r = to_base(x, rate, pitch, BASE_RATE * baud / BAUD)
    z = _low_pass(z, r, 0.75 * baud, int(r / baud * 3) | 1)       # its own width
    sps = r / baud
    power = np.abs(z) ** 2
    n = int((len(z) - 1) / sps) - 1
    if n < 8:
        return np.zeros(0, complex), np.zeros(0), np.zeros(0)
    at = np.arange(n) * sps
    phases = np.arange(16) / 16 * sps
    best = max(phases, key=lambda ph: float(np.interp(at + ph, np.arange(len(power)), power).sum()))
    when = at + best
    s = np.interp(when, np.arange(len(z)), z.real) + 1j * np.interp(when, np.arange(len(z)), z.imag)
    return s, when / r, np.sqrt(np.interp(when - sps / 2, np.arange(len(power)), power))


def read(x: np.ndarray, rate: float, pitch: float, baud: float = BAUD) -> dict:
    """The PSK31 (or PSK63: baud) at about this pitch in x -> {"pitch" (as found), "baud", "eye" (0.64 noise .. 1
    clean: the best second of it), "level", "chars": [(char, when it ended: s into x, how sure 0-1)]}: only the
    letters sent while the eye was EYE_MIN or better."""
    out = {"pitch": pitch, "baud": baud, "eye": 0.0, "level": 0.0, "chars": []}
    for _ in range(2):                               # (once to find what's left of the pitch error, once on pitch)
        s, when, between = _symbols(x, rate, pitch, baud)
        if len(s) < 16:
            return out
        d = s[1:] * np.conj(s[:-1])
        turn = float(np.angle(np.sum(d ** 2)) / 2)   # what the phase moves on by each symbol, turns apart
        off = turn / (2 * np.pi) * baud
        if abs(off) < 0.4 * baud / BAUD:
            break
        pitch += off
    d = d * np.exp(-1j * turn)
    mag = np.abs(d) + 1e-12
    sure = np.abs(d.real) / mag
    bits = d.real > 0
    box = np.ones(min(int(round(EYE_OVER * baud / BAUD)), len(d)))
    eye = np.convolve(np.abs(d.real), box, "same") / np.convolve(mag, box, "same")      # the eye about each symbol
    good = eye >= EYE_MIN
    if good.any():                                   # ...and nothing once the signal has dropped away (its last second's eye lingers)
        power = np.convolve(mag, np.ones(8) / 8, "same")
        good &= power >= 0.25 * np.median(power[good])
    out.update(pitch=float(pitch), eye=float(eye.max()), level=float(np.sqrt(np.mean(np.abs(s[1:][good]) ** 2))) if good.any() else 0.0)
    if not good.any() or np.mean(bits[good]) > TURNS_MIN:
        return out
    turns = good & ~bits                             # (a carrier half the symbol rate off pitch "turns" every time, at full strength)
    if turns.sum() >= 4 and np.mean(between[1:][turns]) > DIP_MAX * np.mean(np.abs(s[1:][turns])):
        return out
    chars, run, start = [], [], None
    zeros = 0                                        # (nothing counts until the first gap between letters has gone by)
    for i, b in enumerate(bits):
        if b:
            if zeros >= 2 and not run:
                start = i
            if zeros == 1 and run:
                run.append(0)
            if start is not None:
                run.append(1)
            zeros = 0
            continue
        zeros += 1
        if zeros == 2 and run:
            c = VARICODE.get("".join(map(str, run)))
            if c in PRINTS and good[start:i + 1].all():
                chars.append((c, float(when[i]), float(np.min(sure[start:i + 1]))))
            run, start = [], None
    out["chars"] = chars
    return out


def spells(r: dict) -> bool:
    """Is what read() (or read_qpsk()) made of a signal PSK, by the one rule every part of the radio goes by -- the
    live reader taking up a new signal (audio/keyboard.py), the survey's finder and What's this? (heard()): at least
    LETTERS_MIN letters, a second or more apart from first to last, nearly all of them printable, and as sure as
    SURE_MIN on average (QPSK31: read_qpsk's own agreement tests stand for that). A hump that merely stands out may be
    FT8 or a whistle; one that spells is PSK."""
    chars = r.get("chars") or []
    if len(chars) < LETTERS_MIN or chars[-1][1] - chars[0][1] < 1.0:
        return False
    said = "".join(c for c, *_ in chars)
    return sum(c in PRINTS for c in said) >= 0.9 * len(said) and (bool(r.get("qpsk")) or float(np.mean([sure for *_, sure in chars])) >= SURE_MIN)


def heard(x: np.ndarray, rate: float, most: int = 3) -> list[dict]:
    """The PSK31 and PSK63 signals in x that say enough letters to be sure of -> [{"hz", "baud" (31 or 63),
    "db" (over the noise), "text"}], the strongest first. (What "What's this?" and the survey's finder go by:
    a hump that merely stands out may be FT8 or a whistle; one that spells is PSK.)"""
    out = []
    for hz, db in find(x, rate, most=most):
        r = max((read(x, rate, hz, baud) for baud in BAUDS), key=lambda r: len(r["chars"]))
        if spells(r):
            said = "".join(c for c, *_ in r["chars"])
            out.append({"hz": float(r["pitch"]), "baud": NAMES[r["baud"]], "db": round(float(db), 1), "text": " ".join(said.split())[:60]})
    return out


# --- QPSK31 --------------------------------------------------------------------------------

def qpsk_send(text: str, rate: float, pitch: float, lead: int = 24, baud: float = BAUD, reverse: bool = False) -> np.ndarray:
    """text as QPSK31 at this pitch (amplitude 1), as heard in upper sideband (reverse: in lower). For the tests."""
    bits = [0] * lead + [int(b) for c in text for b in ENCODE[c] + "00"] + [0] * lead
    turns, sr = [], 0
    for b in bits:
        sr = ((sr << 1) | b) & 31
        turns.append(_QPSK_TURN[_QPSK_OUT[sr]])
    turns = np.array(turns)
    if reverse:
        turns = np.conj(turns)
    phase = np.concatenate(([1 + 0j], np.cumprod(turns)))          # where the phase is in each symbol
    sps = rate / baud
    n = int(len(bits) * sps)
    t = np.arange(n) / sps
    k = np.minimum(t.astype(int), len(bits) - 1)
    w = (1 + np.cos(np.pi * (t - k))) / 2            # from one to the next, as a raised cosine (a half turn goes through nothing)
    z = phase[k] * w + phase[k + 1] * (1 - w)
    return np.real(z * np.exp(2j * np.pi * pitch * np.arange(n) / rate))


def qpsk_bits(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The turns of phase d (complex, one a symbol, as _QPSK_TURN has them) -> (the bits the code was fed, most
    likely; for each symbol, whether those bits coded again give the turn nearest to what was heard)."""
    n = len(d)
    like = np.real(d[:, None] * np.conj(_QPSK_TURN)[None, :])          # how like each of the four each turn is
    states = np.arange(16)
    came = np.stack([((states << 1) | b) & 31 for b in (0, 1)])        # the five bits arrived at, from each state with each bit
    out, nxt = _QPSK_OUT[came], came & 15
    # (what comes into each state: from which two states, with which bit)
    prev = np.zeros((16, 2), int)
    for s0 in states:
        for b in (0, 1):
            t = nxt[b, s0]
            prev[t, s0 >> 3] = s0                   # (the oldest bit of where it came from tells the two apart)
    bit_in = states & 1
    score = np.zeros(16)
    back = np.zeros((n, 16), np.int8)
    for i in range(n):
        cand = score[prev] + like[i][out[bit_in[:, None], prev]]
        back[i] = np.argmax(cand, axis=1)
        score = cand[states, back[i]]
        score -= score.max()
    st = int(np.argmax(score))
    bits, turned = np.zeros(n, np.int8), np.zeros(n, np.int8)
    for i in range(n - 1, -1, -1):
        bits[i] = st & 1
        p = prev[st, back[i, st]]
        turned[i] = _QPSK_OUT[((p << 1) | (st & 1)) & 31]
        st = int(p)
    return bits, turned == np.argmax(like, axis=1)


def read_qpsk(x: np.ndarray, rate: float, pitch: float, baud: float = BAUD, reverse: bool = False) -> dict:
    """The QPSK31 at about this pitch in x (heard in upper sideband; reverse: lower) -> as read(), with "agree" (the
    share of symbols that are what the text read codes back to: the best second of it) for "eye", and "qpsk":
    True. Only the letters sent while that was QPSK_AGREE_MIN or better."""
    out = {"pitch": pitch, "baud": baud, "qpsk": True, "reverse": reverse, "eye": 0.0, "level": 0.0, "chars": []}
    s, when, _ = _symbols(x, rate, pitch, baud)
    if len(s) < 32:
        return out
    d = s[1:] * np.conj(s[:-1])
    if reverse:
        d = np.conj(d)
    fourth = np.sum(d ** 4)
    if abs(fourth) < QPSK_FOURTH_MIN * float(np.sum(np.abs(d) ** 4)):      # (not four places: noise, or something else)
        return out
    turn = float(np.angle(fourth)) / 4               # what's left of a pitch error turns them all alike: to within a quarter turn...
    best = None
    for quarter in range(4):                         # ...and which quarter is the one that makes sense as the code
        dq = d * np.exp(-1j * (turn + quarter * np.pi / 2))
        bits, agree = qpsk_bits(dq)
        if best is None or agree.mean() > best[2].mean():
            best = (quarter, bits, agree)
    quarter, bits, agree = best
    off = (turn + quarter * np.pi / 2) / (2 * np.pi)
    off = (off + 0.5) % 1.0 - 0.5                    # (as a pitch error, the short way round)
    mag = np.abs(d) + 1e-12
    box = np.ones(min(int(round(EYE_OVER * baud / BAUD)), len(d)))
    near = np.convolve(agree.astype(float), box, "same") / np.convolve(np.ones(len(d)), box, "same")
    good = near >= QPSK_AGREE_MIN
    if good.any():                                   # (and nothing once the signal has dropped away)
        power = np.convolve(mag, np.ones(8) / 8, "same")
        good &= power >= 0.25 * np.median(power[good])
    out.update(pitch=float(pitch + (-off if reverse else off) * baud), eye=float(near.max()),
               level=float(np.sqrt(np.mean(np.abs(s[1:][good]) ** 2))) if good.any() else 0.0)
    if not good.any():
        return out
    chars, run, start, zeros = [], [], None, 0       # letters, as read() finds them
    for i, b in enumerate(bits):
        if b:
            if zeros >= 2 and not run:
                start = i
            if zeros == 1 and run:
                run.append(0)
            if start is not None:
                run.append(1)
            zeros = 0
            continue
        zeros += 1
        if zeros == 2 and run:
            c = VARICODE.get("".join(map(str, run)))
            if c in PRINTS and good[start:i + 1].all():
                chars.append((c, float(when[i]), float(near[start:i + 1].min())))
            run, start = [], None
    out["chars"] = chars
    return out
