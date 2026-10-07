"""Sleep Radio receiver: one frequency, tuned like a rig -- sideband speech and Morse.

rtl_airband (the scanner) and rtl_fm only do FM and AM, so this part is our own: the
dongle's raw signal (a quarter of a megahertz of it, from rtl_tcp, which can be retuned
while it runs) is taken apart here.

  - Every 8 ms a 4096-point spectrum is taken of the last 16 ms. That is the waterfall,
    and it's also the first filter: the 192 bins round the wanted frequency, turned back
    into a signal, are that 12 kHz of the band and nothing else, at 12 000 samples a
    second (the windows overlap by half and add up to one, so there's no join to hear).
  - There the passband is moved to the middle, cut out by a sharp filter (2.4 kHz for
    speech, 400 Hz for Morse), and moved to where the ear wants it: speech back to its
    300-2700 Hz, upper or lower sideband the right way up, Morse to a 700 Hz tone.
  - A gain that follows the signal (fast down, slow up) brings a weak station and a
    strong one to the same loudness.

A frequency is the carrier's for sideband (what a rig's dial says) and the signal's own
for Morse. Needs numpy (python3-numpy); a Raspberry Pi 2 gives it about a third of a core.
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
PASS = {"usb": (300.0, 2700.0), "lsb": (-2700.0, -300.0), "cw": (-200.0, 200.0)}     # about the frequency tuned, Hz
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


def centre_for(freq: int) -> int:
    """Where the dongle's centre goes to hear freq: a little above it, on a whole kHz."""
    return (int(freq) + OFF_CENTRE_HZ) // 1000 * 1000


class Tuner:
    """rtl_tcp and a Demod: start(freq, mode), tune(freq, mode) while it runs, stop(). audio(bytes) is called
    with 16-bit audio at AUDIO_RATE, row(bytes) with each waterfall row (about fifteen a second)."""

    def __init__(self, rtl_tcp: str, audio, row, device: int = 0, gain: float = 40.0, ppm: int = 0, port: int = PORT,
                 shift_hz: int = 0) -> None:
        self.rtl_tcp, self.device, self.gain, self.ppm, self.port = rtl_tcp, device, gain, ppm, port
        self.shift_hz = shift_hz                         # what an upconverter adds to every frequency
        self._audio, self._row = audio, row
        self.proc: subprocess.Popen | None = None
        self.centre = 0
        self.freq, self.mode = 0, "usb"
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._demod = Demod()
        self._skip_until = 0.0
        self._stopped = threading.Event()

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
        self.tune(freq, mode, recentre=True)
        threading.Thread(target=self._run, args=(self._sock,), name="tuner", daemon=True).start()

    def tune(self, freq: int, mode: str, recentre: bool = False) -> None:
        with self._lock:
            if recentre or abs(freq - self.centre) > USABLE_HZ:
                self.centre = centre_for(freq)
                self._say(0x01, self.centre + self.shift_hz)
                self._skip_until = time.monotonic() + SETTLE_S
            self.freq, self.mode = int(freq), mode
            self._demod.set(freq - self.centre, mode)

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
            got = b""
            while len(got) < 12:                         # (rtl_tcp's greeting: "RTL0", the tuner, its gains)
                more = sock.recv(12 - len(got))
                if not more:
                    return
                got += more
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
