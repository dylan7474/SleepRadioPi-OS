"""Pictures off the air: slow-scan television (SSTV) and weather fax, read from a receiver's audio.

Both send a picture as a tone whose pitch is the brightness: 1500 Hz is black,
2300 Hz white, and everything between a grey. A line at a time, left to
right, top to bottom, slowly enough to fit a voice channel: a colour SSTV
picture takes from half a minute to two; a weather chart ten or twenty.

SSTV (radio amateurs; 14.230 MHz is the busiest, and the space station
sends them on 145.800):

  * A picture begins with a header that names its mode (the "VIS" code: a
    1900 Hz tone, then eight bits at 1100 and 1300 Hz), so that the
    receiver knows the size and the timing: there are dozens of modes, a
    handful in daily use (MODES below).
  * Every line (or pair of lines) starts with a pulse at 1200 Hz, "blacker
    than black": the sync. The lines are laid out by the clock from the
    header, and nudged to where the sync pulses actually are, so a
    transmitter whose clock is a little off doesn't give a slanted picture.
  * Colour is three scans of each line (green, blue, red: Martin and
    Scottie) or brightness and two colour-difference scans (Robot, PD).

Weather fax (HF radiofax: the German and British weather services send
charts round the clock):

  * 120 lines a minute, in black and white (greys for satellite pictures).
  * A chart is announced by a 300 Hz start tone, then half a minute of
    "phasing" lines -- white with a black bar at the edge -- which say where
    a line begins; a 450 Hz tone ends it.

How the pitch is followed: the sound is moved down so that 1900 Hz (the
middle) is at 0, low-passed, and the pitch is how fast the phase turns from
one sample to the next. A pixel is the mean pitch over its share of the
line.

decode() and the Picture classes here do one picture in sound already
held; the reader that listens for them is audio/pictures.py. send() makes
the sound of a picture, for the tests.
"""
from __future__ import annotations

import numpy as np

SYNC_HZ, BLACK_HZ, WHITE_HZ = 1200.0, 1500.0, 2300.0
CENTRE_HZ = 1900.0           # the middle of it all: also the header's steady tone
VIS_BIT_S = 0.030            # a bit of the header
VIS_ONE_HZ, VIS_ZERO_HZ = 1100.0, 1300.0

# The modes read. kind "gbr": sync, then green, blue, red (Martin: sync first; Scottie: sync before red).
# kind "pd": sync, Y of one line, R-Y, B-Y, Y of the next (two lines of picture to a line sent).
# kind "robot36": sync, Y, then R-Y on even lines and B-Y on odd, at half the width.
# Times in ms: "sync", "porch" (after the sync), "scan" (one colour's sweep), "gap" (between colours).
MODES = {
    44: {"name": "Martin M1", "kind": "martin", "w": 320, "h": 256, "sync": 4.862, "porch": 0.572, "scan": 146.432, "gap": 0.572},
    40: {"name": "Martin M2", "kind": "martin", "w": 320, "h": 256, "sync": 4.862, "porch": 0.572, "scan": 73.216, "gap": 0.572},
    60: {"name": "Scottie S1", "kind": "scottie", "w": 320, "h": 256, "sync": 9.0, "porch": 1.5, "scan": 138.240, "gap": 1.5},
    56: {"name": "Scottie S2", "kind": "scottie", "w": 320, "h": 256, "sync": 9.0, "porch": 1.5, "scan": 88.064, "gap": 1.5},
    76: {"name": "Scottie DX", "kind": "scottie", "w": 320, "h": 256, "sync": 9.0, "porch": 1.5, "scan": 345.6, "gap": 1.5},
    8: {"name": "Robot 36", "kind": "robot36", "w": 320, "h": 240, "sync": 9.0, "porch": 3.0, "scan": 88.0, "gap": 4.5, "porch2": 1.5, "scan2": 44.0},
    93: {"name": "PD 50", "kind": "pd", "w": 320, "h": 256, "sync": 20.0, "porch": 2.08, "scan": 91.52},
    99: {"name": "PD 90", "kind": "pd", "w": 320, "h": 256, "sync": 20.0, "porch": 2.08, "scan": 170.24},
    95: {"name": "PD 120", "kind": "pd", "w": 640, "h": 496, "sync": 20.0, "porch": 2.08, "scan": 121.6},
    98: {"name": "PD 160", "kind": "pd", "w": 512, "h": 400, "sync": 20.0, "porch": 2.08, "scan": 195.584},
    96: {"name": "PD 180", "kind": "pd", "w": 640, "h": 496, "sync": 20.0, "porch": 2.08, "scan": 183.04},
}


def line_s(mode: dict) -> float:
    """How long one line as sent takes (for PD, that is two lines of the picture)."""
    m = mode
    if m["kind"] == "martin":
        return (m["sync"] + m["porch"] + 3 * (m["scan"] + m["gap"])) / 1000
    if m["kind"] == "scottie":
        return (3 * (m["gap"] + m["scan"]) + m["sync"]) / 1000
    if m["kind"] == "robot36":
        return (m["sync"] + m["porch"] + m["scan"] + m["gap"] + m["porch2"] + m["scan2"]) / 1000
    return (m["sync"] + m["porch"] + 4 * m["scan"]) / 1000


def lines_sent(mode: dict) -> int:
    return mode["h"] // 2 if mode["kind"] == "pd" else mode["h"]


def picture_s(mode: dict) -> float:
    return lines_sent(mode) * line_s(mode) + (0.009 if mode["kind"] == "scottie" else 0.0)


def _low_pass(z: np.ndarray, rate: float, cutoff: float, taps: int) -> np.ndarray:
    k = np.arange(taps) - taps // 2
    h = np.sinc(2 * cutoff / rate * k) * np.blackman(taps)
    return np.convolve(z, h / h.sum(), "same")


def pitch(x: np.ndarray, rate: float, width_hz: float = 1000.0) -> tuple[np.ndarray, np.ndarray]:
    """The sound's pitch, sample by sample -> (Hz, how strong). What's further than width_hz from the middle
    (1900 Hz) is filtered away first."""
    t = np.arange(len(x))
    z = x * np.exp(-2j * np.pi * CENTRE_HZ / rate * t)
    z = _low_pass(z, rate, width_hz, 2 * int(rate / width_hz) | 1)       # (short: a longer one smears the sync pulse into the picture's edge)
    turn = np.angle(z[1:] * np.conj(z[:-1]))
    f = CENTRE_HZ + np.concatenate(([0.0], turn)) * rate / (2 * np.pi)
    return f, np.abs(z)


def _means(cum: np.ndarray, starts: np.ndarray, n: float) -> np.ndarray:
    """The mean of a run of n samples from each of `starts` (which needn't be whole), from the running total."""
    a = np.clip(np.rint(starts).astype(int), 0, len(cum) - 1)
    b = np.clip(np.rint(starts + n).astype(int), 0, len(cum) - 1)
    return (cum[b] - cum[a]) / np.maximum(b - a, 1)


def _level(hz: np.ndarray) -> np.ndarray:
    return np.clip((hz - BLACK_HZ) / (WHITE_HZ - BLACK_HZ) * 255.0, 0, 255)


def find_vis(f: np.ndarray, rate: float) -> tuple[int, int] | None:
    """A header in the pitch f -> (the mode's number, the sample the picture begins at), the first there is; None.
    Looked for by its start bit: 1200 Hz for 30 ms after a long 1900, eight more bits of 30 ms, the last 1200."""
    step = max(1, int(rate * 0.005))                 # looked at every 5 ms
    n = len(f) // step
    if n < 140:
        return None
    coarse = f[:n * step].reshape(n, step).mean(axis=1)
    cum = np.concatenate(([0.0], np.cumsum(f)))
    bit = VIS_BIT_S * rate
    lead = int(0.25 / 0.005)
    near = lambda v, hz, by=70.0: abs(v - hz) < by
    for i in range(lead, n - int(0.33 / 0.005)):      # (the whole of the header there, wherever in these 5 ms it began: found
                                                     # the instant its last bit arrived, it was placed 3 ms out)
        if not (near(coarse[i], SYNC_HZ) and near(coarse[i + 2], SYNC_HZ) and near(float(np.median(coarse[i - lead:i - 4])), CENTRE_HZ, 50.0)):
            continue
        if near(coarse[i - 1], SYNC_HZ):             # (not the start of it)
            continue
        # the start bit begins about here: the best fit of the ten bits, within 10 ms either way
        best = None
        for s0 in np.arange((i - 2) * step, (i + 2) * step, max(1, step // 5)):
            mid = _means(cum, s0 + (np.arange(10) + 0.2) * bit, 0.6 * bit)
            if not (near(mid[0], SYNC_HZ, 60) and near(mid[9], SYNC_HZ, 60)):
                continue
            bits = [1 if v < SYNC_HZ else 0 for v in mid[1:9]]
            err = float(np.sum(np.abs(mid[1:9] - np.where(np.array(bits) == 1, VIS_ONE_HZ, VIS_ZERO_HZ))))
            if best is None or err < best[0]:
                best = (err, bits, s0)
        if best is None or best[0] > 8 * 60:
            continue
        _, bits, s0 = best
        code = sum(b << k for k, b in enumerate(bits[:7]))
        if sum(bits) % 2 == 0 and code in MODES:      # (even parity)
            return code, int(round(s0 + 10 * bit))
    return None


class Picture:
    """One SSTV picture being read out of a pitch record: line by line as the sound for each arrives."""

    def __init__(self, code: int, start: int, rate: float) -> None:
        self.code, self.mode, self.rate = code, MODES[code], rate
        self.name = self.mode["name"]
        self.w, self.h = self.mode["w"], self.mode["h"]
        self.rgb = np.zeros((self.h, self.w, 3), np.uint8)
        self.done = 0                                # lines of the picture read so far
        self._line = line_s(self.mode) * rate        # samples to a line as sent
        self._start = float(start) + (0.009 * rate if self.mode["kind"] == "scottie" else 0.0)   # (Scottie: a sync before the first line)
        self._sent = 0                               # lines as sent, read so far
        self._slip = 0.0                             # how far the lines have been nudged, samples
        self._yuv: np.ndarray | None = np.zeros((self.h, self.w, 3), np.float32) if self.mode["kind"] in ("pd", "robot36") else None

    @property
    def finished(self) -> bool:
        return self._sent >= lines_sent(self.mode)

    def moved(self, by: int) -> None:
        """The record has lost its first `by` samples."""
        self._start -= by

    def reach(self) -> int:
        """The sample before which the record is no longer wanted."""
        return max(0, int(self._start + self._slip + self._sent * self._line - 0.05 * self.rate))

    def needs(self) -> int:
        """The sample up to which the pitch must be known for the next line."""
        return int(self._start + self._slip + (self._sent + 1) * self._line + 0.02 * self.rate)

    def _sync_at(self, k: int) -> float:
        """Where line k's sync pulse should begin, in samples."""
        m, ms = self.mode, self.rate / 1000
        at = self._start + self._slip + k * self._line
        return at + (2 * (m["gap"] + m["scan"]) * ms if m["kind"] == "scottie" else 0.0)

    def read(self, f: np.ndarray, cum: np.ndarray) -> int:
        """Read every line whose sound has all come (f: the pitch from sample 0; cum: its running total, one
        longer). -> how many lines of the picture were added."""
        m, ms, before = self.mode, self.rate / 1000, self.done
        while not self.finished and self.needs() <= len(f):
            k = self._sent
            # the sync pulse, where it really is: the lowest 'sync' worth of pitch within a tenth of a line
            want, n = self._sync_at(k), m["sync"] * ms
            reach = min(0.04 * self._line, 8 * ms)
            tries = want + np.linspace(-reach, reach, 33)
            got = _means(cum, tries, n)
            j = int(np.argmin(got))
            if got[j] < (SYNC_HZ + BLACK_HZ) / 2:    # (a pulse is there: a quarter of the way to it, each line)
                self._slip += 0.25 * (tries[j] - want)
            at = self._start + self._slip + k * self._line
            px = lambda start_ms, scan, w=self.w: _level(_means(cum, at + start_ms * ms + np.arange(w) * (scan * ms / w), scan * ms / w))
            if m["kind"] == "martin":
                t0 = m["sync"] + m["porch"]
                g, b, r = (px(t0 + c * (m["scan"] + m["gap"]), m["scan"]) for c in range(3))
                self.rgb[k] = np.stack([r, g, b], axis=1)
                self.done = k + 1
            elif m["kind"] == "scottie":
                g = px(m["gap"], m["scan"])
                b = px(2 * m["gap"] + m["scan"], m["scan"])
                r = px(2 * (m["gap"] + m["scan"]) + m["sync"] + m["porch"], m["scan"])
                self.rgb[k] = np.stack([r, g, b], axis=1)
                self.done = k + 1
            elif m["kind"] == "pd":
                t0 = m["sync"] + m["porch"]
                y1, v, u, y2 = (px(t0 + c * m["scan"], m["scan"]) for c in range(4))
                for row, y in ((2 * k, y1), (2 * k + 1, y2)):
                    self._yuv[row] = np.stack([y, u, v], axis=1)
                    self.rgb[row] = _rgb(y, u, v)
                self.done = 2 * k + 2
            else:                                    # Robot 36: the colour of this line and the one beside it
                y = px(m["sync"] + m["porch"], m["scan"])
                c = np.repeat(px(m["sync"] + m["porch"] + m["scan"] + m["gap"] + m["porch2"], m["scan2"], self.w // 2), 2)
                self._yuv[k, :, 0] = y
                self._yuv[k, :, 2 if k % 2 == 0 else 1] = c        # even: R-Y; odd: B-Y
                pair = k - 1 if k % 2 else k
                for row in range(pair, min(pair + 2, k + 1)):
                    u = self._yuv[pair + 1, :, 1] if pair + 1 <= k else np.full(self.w, 128.0)
                    v = self._yuv[pair, :, 2]
                    self.rgb[row] = _rgb(self._yuv[row, :, 0], u, v)
                self.done = k + 1 if k % 2 else k    # (a line isn't done until the one that shares its colour has come)
            self._sent += 1
        return self.done - before


def _rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Brightness and the two colour differences (B-Y, R-Y, about 128) -> red, green, blue."""
    r = y + 1.402 * (v - 128.0)
    g = y - 0.344136 * (u - 128.0) - 0.714136 * (v - 128.0)
    b = y + 1.772 * (u - 128.0)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def decode(x: np.ndarray, rate: float) -> tuple[str, np.ndarray] | None:
    """The first SSTV picture in sound x -> (its mode's name, the picture as height x width x 3 bytes); None."""
    f, _ = pitch(x, rate)
    found = find_vis(f, rate)
    if found is None:
        return None
    pic = Picture(found[0], found[1], rate)
    pic.read(f, np.concatenate(([0.0], np.cumsum(f))))
    return pic.name, pic.rgb


# --- weather fax ---------------------------------------------------------------------------

FAX_LINE_S = 0.5             # 120 lines a minute
FAX_WIDTH = 1024             # the picture's width here (the service's own is 1810 dots to a line)
FAX_START_HZ, FAX_STOP_HZ = 300.0, 450.0     # the tones before and after a chart: black and white this many times a second
FAX_MOST_LINES = 2400        # twenty minutes: no chart is longer


def fax_tone(f: np.ndarray, rate: float) -> str | None:
    """Is this second or so of pitch a chart's start tone ("start"), its stop tone ("stop"), or neither (None)?"""
    if len(f) < rate * 0.8:
        return None
    d = f - np.mean(f)
    spec = np.abs(np.fft.rfft(d * np.hanning(len(d))))
    hz = np.fft.rfftfreq(len(d), 1 / rate)
    band = (hz > 100) & (hz < 1000)
    if not band.any() or np.std(d) < 150:            # (a tone flipping between black and white swings 400 Hz either way)
        return None
    top = hz[band][np.argmax(spec[band])]
    strong = spec[band].max() > 6 * np.median(spec[band])
    if strong and abs(top - FAX_START_HZ) < 12:
        return "start"
    if strong and abs(top - FAX_STOP_HZ) < 15:
        return "stop"
    return None


def fax_phase(f: np.ndarray, rate: float) -> float | None:
    """Where a line begins, from some seconds of phasing lines (white, with a black bar at the edge, or the other
    way about) -> seconds into f of a line's start; None if they aren't phasing lines."""
    n = int(FAX_LINE_S * rate)
    lines = len(f) // n
    if lines < 6:
        return None
    grid = 200
    idx = (np.arange(grid) * n / grid).astype(int)
    rows = np.array([f[k * n:(k + 1) * n][idx] for k in range(lines)])
    mean = np.median(rows, axis=0)                   # a line, as most of them have it
    white = mean > (BLACK_HZ + WHITE_HZ) / 2
    share = white.mean()
    if not (0.85 <= share <= 0.985 or 0.015 <= share <= 0.15):      # (a bar a twentieth of the line wide, more or less)
        return None
    bar = ~white if share > 0.5 else white
    where = np.flatnonzero(bar)
    # the bar's middle, on the ring (it may straddle the end of the line as cut here); a line begins where the bar does
    ang = np.angle(np.sum(np.exp(2j * np.pi * where / grid)))
    return float(((ang / (2 * np.pi)) - bar.mean() / 2) % 1.0 * FAX_LINE_S)


class Fax:
    """A weather chart being read out of a pitch record: grey lines, half a second each."""

    name = "Weather fax"

    def __init__(self, start: float, rate: float) -> None:
        self.rate, self.w = rate, FAX_WIDTH
        self.grey = np.zeros((0, FAX_WIDTH), np.uint8)
        self.done = 0
        self._start = float(start)                   # the sample the first line begins at
        self._line = FAX_LINE_S * rate
        self.finished = False

    @property
    def h(self) -> int:
        return self.done

    @property
    def rgb(self) -> np.ndarray:
        return self.grey

    def moved(self, by: int) -> None:
        self._start -= by

    def reach(self) -> int:
        return max(0, int(self._start + self.done * self._line) - 2)

    def rephase(self, by_s: float) -> None:
        """Lines begin this much later than was thought: what's read so far is turned round to match."""
        shift = int(round(by_s / FAX_LINE_S * self.w)) % self.w
        if shift:
            self.grey = np.roll(self.grey, -shift, axis=1)
            self._start += by_s * self.rate

    def needs(self) -> int:
        return int(self._start + (self.done + 1) * self._line) + 1

    def read(self, f: np.ndarray, cum: np.ndarray) -> int:
        before, rows = self.done, []
        while not self.finished and self.needs() <= len(f):
            at = self._start + self.done * self._line
            rows.append(_level(_means(cum, at + np.arange(self.w) * (self._line / self.w), self._line / self.w)).astype(np.uint8))
            self.done += 1
            if self.done >= FAX_MOST_LINES:
                self.finished = True
        if rows:
            self.grey = np.vstack([self.grey, *[r[None, :] for r in rows]])
        return self.done - before


# --- making the sound of a picture (for the tests) -----------------------------------------

def _tone(freqs: np.ndarray, rate: float) -> np.ndarray:
    return np.cos(2 * np.pi * np.cumsum(freqs) / rate)


def send(code: int, rgb: np.ndarray, rate: float, lead_s: float = 0.5) -> np.ndarray:
    """A picture (height x width x 3 bytes, the mode's size) as the sound of that SSTV mode, header and all."""
    m, ms = MODES[code], rate / 1000
    parts: list[tuple[float, float | np.ndarray]] = [(lead_s * 1000, 0.0), (300, CENTRE_HZ), (10, SYNC_HZ), (300, CENTRE_HZ), (30, SYNC_HZ)]
    bits = [(code >> k) & 1 for k in range(7)]
    bits.append(sum(bits) % 2)
    parts += [(30, VIS_ONE_HZ if b else VIS_ZERO_HZ) for b in bits] + [(30, SYNC_HZ)]
    hz = lambda row: BLACK_HZ + row.astype(float) / 255.0 * (WHITE_HZ - BLACK_HZ)
    img = rgb.astype(np.float32)
    y = 0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]
    u = np.clip(128 + (img[..., 2] - y) / 1.772, 0, 255)
    v = np.clip(128 + (img[..., 0] - y) / 1.402, 0, 255)
    if m["kind"] == "scottie":
        parts.append((9.0, SYNC_HZ))
    for k in range(lines_sent(m)):
        if m["kind"] == "martin":
            parts += [(m["sync"], SYNC_HZ), (m["porch"], BLACK_HZ)]
            for c in (1, 2, 0):
                parts += [(m["scan"], hz(rgb[k, :, c])), (m["gap"], BLACK_HZ)]
        elif m["kind"] == "scottie":
            parts += [(m["gap"], BLACK_HZ), (m["scan"], hz(rgb[k, :, 1])), (m["gap"], BLACK_HZ), (m["scan"], hz(rgb[k, :, 2])),
                      (m["sync"], SYNC_HZ), (m["porch"], BLACK_HZ), (m["scan"], hz(rgb[k, :, 0]))]
        elif m["kind"] == "pd":
            uu, vv = (u[2 * k] + u[2 * k + 1]) / 2, (v[2 * k] + v[2 * k + 1]) / 2
            parts += [(m["sync"], SYNC_HZ), (m["porch"], BLACK_HZ), (m["scan"], hz(y[2 * k])), (m["scan"], hz(vv)), (m["scan"], hz(uu)), (m["scan"], hz(y[2 * k + 1]))]
        else:
            pair = k - 1 if k % 2 else k
            c = (v if k % 2 == 0 else u)[pair:pair + 2].mean(axis=0).reshape(-1, 2).mean(axis=1)
            parts += [(m["sync"], SYNC_HZ), (m["porch"], BLACK_HZ), (m["scan"], hz(y[k])), (m["gap"], BLACK_HZ if k % 2 == 0 else WHITE_HZ),
                      (m["porch2"], CENTRE_HZ), (m["scan2"], hz(c))]
    parts.append((300, 0.0))
    # each part's pitch, sample by sample, with the parts' ends kept exact (the lengths aren't whole samples)
    ends = np.cumsum([p[0] for p in parts]) * ms
    total = int(ends[-1])
    out = np.zeros(total)
    amp = np.ones(total)
    a = 0.0
    for (dur, what), b in zip(parts, ends):
        i, j = int(round(a)), min(total, int(round(b)))
        if j > i:
            if isinstance(what, np.ndarray):
                out[i:j] = what[np.minimum((np.arange(i, j) - a) * len(what) / (b - a), len(what) - 1).astype(int)]
            elif what == 0.0:
                amp[i:j] = 0.0
                out[i:j] = CENTRE_HZ
            else:
                out[i:j] = what
        a = b
    return _tone(out, rate) * amp


def send_fax(grey: np.ndarray, rate: float, phasing_s: float = 10.0, start_s: float = 3.0, bar_first: bool = True) -> np.ndarray:
    """A grey picture (lines x any width, bytes) as the sound of a weather chart: start tone, phasing lines (white,
    with a black bar at the start of each), the chart, the stop tone."""
    n = int(FAX_LINE_S * rate)
    square = lambda hz, secs: np.where(np.sin(2 * np.pi * hz * np.arange(int(secs * rate)) / rate) >= 0, WHITE_HZ, BLACK_HZ)
    phasing = np.full(n, WHITE_HZ)
    phasing[:n // 20] = BLACK_HZ
    rows = [BLACK_HZ + grey[k][(np.arange(n) * grey.shape[1] // n)].astype(float) / 255.0 * (WHITE_HZ - BLACK_HZ) for k in range(grey.shape[0])]
    f = np.concatenate([square(FAX_START_HZ, start_s), np.tile(phasing, int(phasing_s / FAX_LINE_S)), *rows, square(FAX_STOP_HZ, 3.0)])
    return _tone(f, rate)
