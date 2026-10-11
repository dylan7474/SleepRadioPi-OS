"""Sleep Radio receiver: one frequency, tuned like a rig -- sideband speech, Morse and short-wave AM.

rtl_airband (the scanner) and rtl_fm only do FM and AM, so this part is our own: the
dongle's raw signal (a quarter of a megahertz of it, from rtl_tcp, which can be retuned
while it runs) is taken apart here.

  - Every 8 ms a 4096-point spectrum is taken of the last 16 ms. That is the waterfall,
    and it's also the first filter: the 192 bins round the wanted frequency, turned back
    into a signal, are that 12 kHz of the band and nothing else, at 12 000 samples a
    second (the windows overlap by half and add up to one, so there's no join to hear).
  - There the passband is moved to the middle, cut out by a sharp filter (2.4 kHz for
    speech, 400 Hz for Morse), and moved to where the ear wants it: speech back to its
    300-2700 Hz, upper or lower sideband the right way up, Morse to a 700 Hz tone. AM (a
    broadcast station on short or medium wave: 9 kHz of it) is the strength of what's
    left, less its steady part, which is the carrier.
  - A gain that follows the signal (fast down, slow up) brings a weak station and a
    strong one to the same loudness.

A frequency is the carrier's for sideband and AM (what a rig's dial says) and the signal's
own for Morse. Short wave reaches an RTL-SDR through an upconverter, which adds its own
oscillator's frequency to everything (shift_hz); what that and the dongle are out by
between them, measured on a station of known frequency, is taken off again (trim_hz). Needs numpy (python3-numpy); a Raspberry Pi 2 gives it about a third of a core.
"""

from __future__ import annotations

import logging
import socket
import struct
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("receiver")

FS = 256_000                 # what the dongle is asked for
N = 4096                     # the spectrum's size: 62.5 Hz a bin
HOP = N // 2
M = 192                      # the bins kept: FS * M / N = 12 kHz
AUDIO_RATE = FS * M // N     # 12 000
BIN = FS / N
PASS = {"usb": (300.0, 2700.0), "lsb": (-2700.0, -300.0), "cw": (-200.0, 200.0), "am": (-4500.0, 4500.0)}     # about the frequency tuned, Hz
MODES = tuple(PASS)
CW_PITCH = 700.0             # Morse is heard at this pitch
TAPS = 193                   # the passband's filter, at 12 kHz: about 150 Hz from passed to stopped
USABLE_HZ = 100_000          # either side of the dongle's centre: beyond it the centre is moved
OFF_CENTRE_HZ = 24_000       # ...to this far above the frequency (an RTL-SDR has a false carrier at its exact centre)
HOPS = 8                     # worked on at a time: 64 ms
SPECTRUM_BINS = 1024         # the waterfall's row (four bins to one)
ZERO_DB, PER_DB = -110.0, 2.5      # a row's bytes: 0 is this many dB (of full scale, a bin), this many steps a dB
AGC_TARGET, AGC_MAX, AGC_RELEASE_S = 0.25, 3000.0, 1.5
PORT = 47_100                # rtl_tcp, on this machine only
SETTLE_S = 0.08              # after the centre moves: what arrives this long isn't used (the tuner settling)

_WIN = np.sin(np.pi * (np.arange(N) + 0.5) / N).astype(np.float32)        # (its square, half-overlapped, adds up to one)
_WIN_OUT = np.sin(np.pi * (np.arange(M) + 0.5) / M).astype(np.float32)


def lowpass(half_hz: float, rate: float = AUDIO_RATE, taps: int = TAPS) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * half_hz / rate * n) * np.blackman(taps)
    return (h / h.sum()).astype(np.float32)


class Demod:
    """The dongle's signal (complex, FS a second, a whole number of HOPs at a time) -> the audio of one
    frequency in it, and the spectrum of all of it."""

    def __init__(self) -> None:
        self._tail = np.zeros(N - HOP, np.complex64)
        self._half = np.zeros(M // 2, np.complex64)      # the second half of the last piece, to add to the next
        self._hist = np.zeros(TAPS - 1, np.complex64)    # (the filter's memory)
        self._n = 0                                      # samples of audio made: the oscillators' clock
        self._gain = 1.0
        self._env = 0.0
        self._carrier = 0.0                              # AM: the steady part of the signal's strength
        self.level_db = -120.0                           # the passband's strength, dB of full scale
        self.set(0.0, "usb")

    def set(self, offset_hz: float, mode: str) -> None:
        """Listen offset_hz from the centre of what's coming in."""
        lo, hi = PASS[mode]
        mid = offset_hz + (lo + hi) / 2                  # the passband's middle, in what's coming in
        self._k0 = 2 * int(round(mid / (2 * BIN)))       # the bins taken are centred here (an even bin: the pieces join without a turn)
        self._fine = mid - self._k0 * BIN                # ...which leaves it this far from the middle of them
        self._out = CW_PITCH if mode == "cw" else (lo + hi) / 2
        self._h = lowpass((hi - lo) / 2 + 40.0)
        self.mode = mode
        self._pick = (self._k0 + np.arange(-M // 2, M // 2)) % N
        self._place = np.arange(-M // 2, M // 2) % M

    def process(self, iq: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """-> (audio, int16; the mean power in each of N bins, lowest frequency first)."""
        x = np.concatenate([self._tail, iq])
        self._tail = x[len(x) - (N - HOP):]
        frames = np.lib.stride_tricks.sliding_window_view(x, N)[::HOP]
        X = np.fft.fft(frames * _WIN, axis=1)
        power = np.fft.fftshift((X.real ** 2 + X.imag ** 2).mean(axis=0)) / (N * N / 4)
        Y = np.zeros((len(X), M), np.complex64)
        Y[:, self._place] = X[:, self._pick]
        y = np.fft.ifft(Y, axis=1) * (M / N) * _WIN_OUT
        z = np.empty(len(y) * (M // 2), np.complex64)
        for i, piece in enumerate(y):                    # each piece overlaps the last by half
            z[i * (M // 2):(i + 1) * (M // 2)] = self._half + piece[:M // 2]
            self._half = piece[M // 2:]
        t = (self._n + np.arange(len(z))) / AUDIO_RATE
        self._n += len(z)
        z = z * np.exp(-2j * np.pi * self._fine * t)     # the passband to the middle...
        zz = np.concatenate([self._hist, z])
        self._hist = zz[len(zz) - (TAPS - 1):]
        z = np.convolve(zz, self._h, "valid")            # ...cut out...
        self.level_db = float(10 * np.log10(np.mean(z.real ** 2 + z.imag ** 2) + 1e-12))
        if self.mode == "am":                            # ...and its strength, less the carrier, is the programme
            env = np.abs(z)
            was, self._carrier = self._carrier, self._carrier + 0.2 * (float(env.mean()) - self._carrier)
            a = env - np.linspace(was, self._carrier, len(env), endpoint=False)
        else:
            a = (z * np.exp(2j * np.pi * self._out * t)).real    # ...and to where the ear wants it
        return self._agc(a), power

    def _agc(self, a: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(a))) if len(a) else 0.0
        self._env = max(peak, self._env * float(np.exp(-len(a) / AUDIO_RATE / AGC_RELEASE_S)))
        gain = min(AGC_MAX, AGC_TARGET / max(self._env, 1e-9))
        ramp = np.linspace(self._gain, gain, len(a), endpoint=False) if gain > self._gain else np.full(len(a), gain)
        self._gain = gain
        return np.clip(a * ramp * 32767.0, -32768, 32767).astype("<i2")


def spectrum_row(power: np.ndarray) -> bytes:
    """A spectrum (N bins) -> the waterfall's row: a byte a bin from the lowest frequency to the highest."""
    p = power.reshape(SPECTRUM_BINS, -1).mean(axis=1)
    return np.clip((10 * np.log10(p + 1e-14) - ZERO_DB) * PER_DB, 0, 255).astype(np.uint8).tobytes()


R820T, R820T_XTAL_HZ = 5, 28_800_000      # rtl_tcp's number for the tuner chip; its crystal


def r820t_if(rate: int) -> int:
    """Where an R820T puts the signal for the dongle to take it from, Hz: it depends on how wide a filter the
    sample rate asks for (the driver's r82xx_set_bandwidth; 2.125 MHz at the 256 kHz used here)."""
    table = (1_700_000, 1_600_000, 1_550_000, 1_450_000, 1_200_000, 900_000, 700_000, 550_000, 450_000, 350_000)
    hp1, hp2 = 350_000, 380_000
    if rate > 6_000_000:
        return 4_570_000
    if rate > table[0] + hp1 + hp2:
        return 3_570_000
    bw, at, real = rate, 2_300_000, 0
    if bw > table[0] + hp1:
        bw, at, real = bw - hp2, at + hp2, real + hp2
    if bw > table[0]:
        bw, at, real = bw - hp1, at + hp1, real + hp1
    i = next((k for k, t in enumerate(table) if bw > t), len(table)) - 1
    return at - (real + table[i]) // 2


R820T_IF_HZ = r820t_if(FS)


def driver() -> int:
    """Which librtlsdr is on this computer: 0 for the 0.6 line, 2 for 2.0 and after, -1 if it can't be told (they
    set the R820T's oscillator by different sums: r820t_off). From the library's file name, librtlsdr.so.2.0.1."""
    import glob
    import re
    found = []
    for pattern in ("/usr/lib/*/librtlsdr.so.*", "/usr/lib/librtlsdr.so.*", "/usr/local/lib/librtlsdr.so.*", "/usr/lib64/librtlsdr.so.*"):
        for path in glob.glob(pattern):
            m = re.search(r"\.so\.(\d+)\.(\d+)", path)
            if m:
                found.append(int(m.group(1)))
    return (2 if max(found) >= 2 else 0) if found else -1


def r820t_off(freq: int, ppm: int = 0, driver: int = 0) -> float:
    """How far from `freq` (Hz) an R820T dongle really goes when it's asked for it. `driver`: which librtlsdr
    (driver()). The chip's oscillator moves in steps, and from 2.0 on the driver takes the nearest: 27 Hz either
    way at most, at 140 MHz. Measured on one carrier from seven places of the dongle's centre: 124 Hz apart, 17
    with this taken off. Before 2.0 it did worse, as follows. The chip's oscillator moves in
    steps (55 Hz at 140 MHz, where an upconverter puts short wave; coarser higher up), and the driver (librtlsdr's
    r82xx_set_pll, followed here sum for sum) first drops what's under a kHz of the oscillator's own frequency. So
    two frequencies a few kHz apart could be 140 Hz apart in their errors: one broadcast carrier heard from seven
    places of the dongle's centre read 136 Hz apart, and 15 Hz apart with this taken off. Known, it's taken off
    again where the signal is picked out."""
    xtal = int(R820T_XTAL_HZ * (1.0 + ppm / 1e6))       # (the driver's crystal, with the correction it was given)
    lo = int(freq) + R820T_IF_HZ
    lo_khz, xtal_khz = (lo + 500) // 1000, (xtal + 500) // 1000
    mix_div = 2
    while mix_div <= 64 and not 1_770_000 <= lo_khz * mix_div < 3_540_000:
        mix_div <<= 1
    if mix_div > 64 or driver < 0:
        return 0.0                                   # (out of the chip's reach: the driver gives up too; or a driver not known)
    vco = lo * mix_div
    if driver >= 2:                                  # the nearest step: 65536 of them to twice the crystal
        return 2 * xtal * ((xtal + 65536 * vco) // (2 * xtal)) / 65536 / mix_div - lo
    nint = vco // (2 * xtal)
    fra = (vco - 2 * xtal * nint) // 1000            # the part of a step, in kHz: what's under a kHz is dropped
    n_sdm, sdm = 2, 0
    while fra > 1:
        if fra > 2 * xtal_khz // n_sdm:
            sdm += 32768 // (n_sdm // 2)
            fra -= 2 * xtal_khz // n_sdm
            if n_sdm >= 0x8000:
                break
        n_sdm <<= 1
    return 2 * xtal * (nint + sdm / 65536) / mix_div - lo


def centre_for(freq: int) -> int:
    """Where the dongle's centre goes to hear freq: a little above it, on a whole kHz."""
    return (int(freq) + OFF_CENTRE_HZ) // 1000 * 1000


class Tuner:
    """rtl_tcp and a Demod: start(freq, mode), tune(freq, mode) while it runs, stop(). audio(bytes) is called
    with 16-bit audio at AUDIO_RATE, row(bytes) with each waterfall row (about fifteen a second)."""

    def __init__(self, rtl_tcp: str, audio, row, device: int = 0, gain: float = 40.0, ppm: int = 0, port: int = PORT,
                 shift_hz: int = 0, trim_hz: int = 0) -> None:
        self.rtl_tcp, self.device, self.gain, self.ppm, self.port = rtl_tcp, device, gain, ppm, port
        self.shift_hz = shift_hz + trim_hz               # what an upconverter adds to every frequency, and what it's out by
        self._audio, self._row = audio, row
        self.proc: subprocess.Popen | None = None
        self.centre = 0
        self.freq, self.mode = 0, "usb"
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._demod = Demod()
        self._skip_until = 0.0
        self._stopped = threading.Event()
        self.chip = 0                                    # the tuner chip, as rtl_tcp names it (R820T: its steps are allowed for)
        self.driver = driver()                           # ...by the sums of the librtlsdr that's here
        self.off = 0.0                                   # how far from where it was asked the dongle's centre really is

    @property
    def level_db(self) -> float:
        return self._demod.level_db

    def _say(self, command: int, value: int) -> None:
        if self._sock is not None:
            self._sock.sendall(struct.pack(">BI", command, value & 0xFFFFFFFF))

    def start(self, freq: int, mode: str) -> None:
        self.centre = centre_for(freq)
        self.proc = subprocess.Popen([self.rtl_tcp, "-a", "127.0.0.1", "-p", str(self.port), "-d", str(self.device),
                                      "-f", str(self.centre + self.shift_hz), "-s", str(FS), "-g", f"{self.gain:.1f}",
                                      "-P", str(self.ppm)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 8.0
        while True:                                      # (it takes a second or two to open the dongle and listen)
            try:
                self._sock = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
                break
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    raise ValueError("the dongle didn't start (is something else using it?)") from None
                time.sleep(0.2)
        self._sock.settimeout(5.0)
        got = b""
        try:                                             # (rtl_tcp's greeting: "RTL0", the tuner chip, its gains)
            while len(got) < 12:
                more = self._sock.recv(12 - len(got))
                if not more:
                    break
                got += more
        except OSError:
            pass
        if len(got) < 12:
            raise ValueError("the dongle didn't start (is something else using it?)")
        self.chip = struct.unpack(">4sII", got)[1] if got[:4] == b"RTL0" else 0
        self.tune(freq, mode, recentre=True)
        threading.Thread(target=self._run, args=(self._sock,), name="tuner", daemon=True).start()

    def tune(self, freq: int, mode: str, recentre: bool = False) -> None:
        with self._lock:
            if recentre or abs(freq - self.centre) > USABLE_HZ:
                self.centre = centre_for(freq)
                self._say(0x01, self.centre + self.shift_hz)
                self._skip_until = time.monotonic() + SETTLE_S
            self.freq, self.mode = int(freq), mode
            self.off = r820t_off(self.centre + self.shift_hz, self.ppm, self.driver) if self.chip == R820T else 0.0
            self._demod.set(freq - self.centre - self.off, mode)

    def stop(self) -> None:
        self._stopped.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self, sock: socket.socket) -> None:
        want, buf = 2 * HOP * HOPS, b""
        try:
            while not self._stopped.is_set():
                more = sock.recv(65536)
                if not more:
                    break
                buf += more
                if len(buf) > 8 * want:                  # (fallen behind: what's oldest goes)
                    buf = buf[len(buf) - want:]
                    buf = buf[len(buf) % 2:]
                while len(buf) >= want:
                    raw, buf = buf[:want], buf[want:]
                    if time.monotonic() < self._skip_until:
                        continue
                    iq = ((np.frombuffer(raw, np.uint8).astype(np.float32) - 127.5) / 127.5).view(np.complex64)
                    with self._lock:
                        audio, power = self._demod.process(iq)
                    self._audio(audio.tobytes())
                    self._row(spectrum_row(power))
        except OSError:
            pass
