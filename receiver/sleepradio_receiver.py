#!/usr/bin/env python3
"""Sleep Radio receiver: an RTL-SDR dongle as a radio station on the network.

Runs on a small computer next to the aerial (a Raspberry Pi 2 is plenty) and
turns its dongle into something a Sleep Radio -- or any internet radio player
-- can tune in to as an ordinary station:

    http://RECEIVER:8074/audio?band=2m          every channel of a band at once
    http://RECEIVER:8074/audio?freq=145.5       one frequency (narrow FM)
    http://RECEIVER:8074/audio?freq=95.0&mode=wfm   broadcast FM

A band is watched all at once (rtl_airband takes the whole 2.4 MHz the dongle
hears apart into its channels, each with its own squelch), so it's a scanner
that never misses the start of a call: whichever channel opens is what you
hear, a priority channel takes over from the others, and the stream carries
the channel's name as its "now playing" title. With nobody transmitting the
stream is silence, so a player stays tuned in.

The audio is a WAV stream (16-bit mono) with ICY titles. /status says what
it's doing as JSON; /bands lists the bands. One dongle does one thing at a
time: the latest request wins, and listeners to what it was doing before are
disconnected. With no listener for a while the dongle is left to rest.

Only the Python standard library is needed, plus rtl_airband (built with NFM)
and, for broadcast FM, rtl_fm from the rtl-sdr package.
"""

from __future__ import annotations

import argparse
import array
import json
import logging
import math
import queue
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("receiver")

PORT = 8074
RATE = 16_000                # rtl_airband's audio rate when built with NFM
BATCH = RATE // 8            # ...and its packet: 125 ms
WFM_RATE = 32_000            # rtl_fm's broadcast FM
CHUNK_BYTES = 4000           # one packet of 16-bit audio = the ICY interval
SAMPLE_RATE_MS = 2.4         # what the dongle is asked for (MS/s)
USABLE = 0.8                 # of that, the part away from the filter's edges
FFT_SIZE = 512               # a Pi 2 keeps up with 512 (1024 and 2048 overflowed)
UDP_BASE = 47_000            # rtl_airband sends channel i's audio to this + i
HANG_S = 2.0                 # stay with a channel this long after it closes, for the reply
IDLE_S = 60.0                # no listener this long: the dongle rests
GAIN_DB = 40.0
VOLUME = 1.0

# --- bands -----------------------------------------------------------------------------


def marine_channels() -> list[dict]:
    """International marine VHF, the ship side (156.000-157.425 MHz: simplex
    channels, and the ships' half of the duplex ones), less the DSC data
    channel (70) and the two guard channels beside 16."""
    names = {0: "Search and rescue", 6: "Ship to ship", 8: "Ship to ship", 12: "Port operations",
             13: "Bridge to bridge", 14: "Port operations", 16: "Distress and calling", 67: "Coastguard",
             72: "Ship to ship", 77: "Ship to ship", 80: "Marinas"}
    out = []
    for n in [0, *range(1, 29), *range(60, 89)]:
        if n in (70, 75, 76):
            continue
        hz = 156_000_000 if n == 0 else (156_050_000 + 50_000 * (n - 1) if n < 60 else 156_025_000 + 50_000 * (n - 60))
        out.append({"freq": hz, "name": f"Channel {n}" + (f", {names[n]}" if n in names else ""),
                    **({"priority": True} if n == 16 else {})})
    return sorted(out, key=lambda c: c["freq"])


def two_metre_channels(names: dict[int, str] | None = None, skip: tuple[int, ...] = ()) -> list[dict]:
    """The 2 m band's FM part: 145.200-145.7875 MHz every 12.5 kHz (simplex,
    then the repeater outputs) and 145.800 (the space station)."""
    named = {145_500_000: "Calling channel", 145_800_000: "Space station", **(names or {})}
    out = []
    for hz in [*range(145_200_000, 145_800_000, 12_500), 145_800_000]:
        if hz in skip:
            continue
        mhz = f"{hz / 1e6:.4f}".rstrip("0")
        out.append({"freq": hz, "name": mhz + (f", {named[hz]}" if hz in named else ""),
                    **({"priority": True} if hz == 145_500_000 else {})})
    return out


# The repeaters and gateways within about 50 km of Guisborough (from the repeater list
# OpenWebRX kept, 2026-10-04); GB7PB on 145.6625 is D-STAR -- a buzz in FM -- so it's left out.
NEAR_GUISBOROUGH = {145_212_500: "MB7ICC gateway", 145_237_500: "MB7IMB gateway", 145_312_500: "MB7ISQ gateway",
                    145_600_000: "GB7RW", 145_625_000: "GB3HG", 145_737_500: "GB3RT", 145_762_500: "GB3IR"}

DEFAULT_BANDS = {
    "2m": {"name": "2 metres", "mode": "nfm", "channels": two_metre_channels(NEAR_GUISBOROUGH, skip=(145_662_500,))},
    "marine": {"name": "Marine VHF", "mode": "nfm", "channels": marine_channels()},
}


def load_bands(path: Path | None) -> dict:
    """The built-in bands, with a JSON file's on top ({"id": {"name", "mode", "channels": [{"freq", "name"}]}})."""
    bands = {k: dict(v) for k, v in DEFAULT_BANDS.items()}
    if path is not None and path.is_file():
        for key, band in json.loads(path.read_text()).items():
            chans = [{"freq": int(c["freq"]), "name": str(c.get("name") or f"{int(c['freq']) / 1e6:.4f}"),
                      **({"priority": True} if c.get("priority") else {})} for c in band["channels"]]
            bands[str(key)] = {"name": str(band.get("name") or key), "mode": band.get("mode", "nfm"), "channels": chans}
    return bands


# --- what to listen to -----------------------------------------------------------------


def parse_spec(query: dict, bands: dict) -> dict:
    """?band=2m | ?freq=145.5[&mode=nfm|am|wfm][&squelch=off] -> a spec; ValueError says what's wrong.
    freq is in MHz (145.5) or Hz (145500000)."""
    one = lambda k: (query.get(k) or [None])[0] if isinstance(query.get(k), list) else query.get(k)
    band, freq = one("band"), one("freq")
    if band:
        if band not in bands:
            raise ValueError(f"no band called {band}: there is {', '.join(sorted(bands))}")
        return {"band": band}
    if freq is None:
        raise ValueError("say what to listen to: ?band=NAME or ?freq=MHZ")
    try:
        f = float(freq)
    except (TypeError, ValueError):
        raise ValueError("freq is a number: 145.5 (MHz) or 145500000 (Hz)") from None
    hz = int(round(f * 1e6)) if f < 100_000 else int(round(f))
    if not 24_000_000 <= hz <= 1_766_000_000:
        raise ValueError("an RTL-SDR tunes from 24 to 1766 MHz")
    mode = (one("mode") or "nfm").lower()
    if mode not in ("nfm", "am", "wfm"):
        raise ValueError("mode is nfm, am or wfm")
    spec = {"freq": hz, "mode": mode}
    if mode != "wfm" and str(one("squelch") or "").lower() in ("off", "0", "false", "no"):
        spec["squelch"] = False
    return spec


def spec_channels(spec: dict, bands: dict) -> tuple[str, list[dict]]:
    """(what to call it, its channels)."""
    if "band" in spec:
        return bands[spec["band"]]["name"], bands[spec["band"]]["channels"]
    mhz = f"{spec['freq'] / 1e6:.4f}".rstrip("0").rstrip(".")
    return f"{mhz} MHz", [{"freq": spec["freq"], "name": f"{mhz} MHz"}]


def centre_for(freqs: list[int], rate_hz: float = SAMPLE_RATE_MS * 1e6) -> int:
    """Where to put the dongle's centre. An RTL-SDR has a false carrier at its
    exact centre, so it goes as far from any channel as it can -- 100 kHz clear
    is plenty -- while every channel stays inside the part of the band the
    dongle hears well; of the places that do, the nearest to the middle.
    ValueError if the channels are too far apart for one dongle."""
    lo, hi = min(freqs), max(freqs)
    half = int(rate_hz * USABLE / 2)
    if hi - lo > 2 * half:
        raise ValueError(f"these channels span {(hi - lo) / 1e6:.2f} MHz: one dongle hears {2 * half / 1e6:.2f} MHz at a time")
    mid = (lo + hi) // 2
    spots = [(min(abs(c - f) for f in freqs), c) for c in range(hi - half, lo + half + 1, 500)]
    want = min(100_000, max(gap for gap, _ in spots))
    return min((abs(c - mid), c) for gap, c in spots if gap >= want)[1]


def airband_conf(channels: list[dict], mode: str = "nfm", squelch: bool = True, gain: float = GAIN_DB,
                 ppm: int = 0, device: int = 0, udp_base: int = UDP_BASE) -> str:
    """rtl_airband's configuration: every channel at once, each one's audio to
    its own UDP port on this machine, sent only while its squelch is open."""
    centre = centre_for([c["freq"] for c in channels])
    rows = []
    for i, c in enumerate(channels):
        sq = "" if squelch else " squelch_threshold = -100;"
        rows.append(f'    {{ freq = {c["freq"] / 1e6:.6f}; modulation = "{mode}";{sq} outputs: ({{ type = "udp_stream"; '
                    f'dest_address = "127.0.0.1"; dest_port = {udp_base + i}; continuous = false; }}); }}')
    return (f"fft_size = {FFT_SIZE};\ndevices: ({{\n  type = \"rtlsdr\"; index = {device}; gain = {gain:.1f}; correction = {ppm};\n"
            f"  centerfreq = {centre / 1e6:.6f}; sample_rate = {SAMPLE_RATE_MS}; mode = \"multichannel\";\n  channels: (\n"
            + ",\n".join(rows) + "\n  );\n});\n")


# --- audio -----------------------------------------------------------------------------


def wav_header(rate: int) -> bytes:
    """A WAV header for a stream with no end (16-bit mono)."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def icy_block(title: str) -> bytes:
    """An ICY metadata block: a length byte (in 16s), then StreamTitle='...';"""
    text = ("StreamTitle='" + title.replace("'", "’").replace(";", ",") + "';").encode("utf-8")[:4000]
    text += b"\0" * (-len(text) % 16)
    return bytes([len(text) // 16]) + text


def to_pcm(floats: bytes, volume: float = VOLUME) -> tuple[bytes, float]:
    """rtl_airband's 32-bit floats -> 16-bit audio, and its level (RMS, 0-1)."""
    x = array.array("f")
    x.frombytes(floats[:len(floats) // 4 * 4])
    if sys.byteorder != "little":
        x.byteswap()
    k = 32767.0 * volume
    out = array.array("h", [32767 if (v := s * k) > 32767 else -32768 if v < -32768 else int(v) for s in x])
    if sys.byteorder != "little":
        out.byteswap()
    rms = math.sqrt(sum(s * s for s in x) / len(x)) if len(x) else 0.0
    return out.tobytes(), rms


class Mixer:
    """Which channel is on air, and the stream's timekeeping.

    packet(i, audio, now) is a channel's audio arriving (only open channels
    send any): it comes back as what to play if that channel has the air.
    The first to speak takes it and keeps it until HANG_S after its last
    packet; a priority channel takes it from one that isn't. filler(now) is
    silence, whenever the stream has fallen behind real time -- so a player
    hears a station that's simply quiet, and never one that has stalled."""

    def __init__(self, channels: list[dict], rate: int = RATE, hang_s: float = HANG_S) -> None:
        self.channels, self.rate, self.hang_s = channels, rate, hang_s
        self.current: int | None = None
        self.last = [0.0] * len(channels)           # when each channel last sent audio
        self.level = [0.0] * len(channels)
        self.t0: float | None = None
        self.sent = 0                               # samples sent since t0

    def _open(self, i: int, now: float) -> bool:
        return self.last[i] > 0 and now - self.last[i] <= self.hang_s

    def active(self, now: float, within: float = 0.4) -> list[int]:
        return [i for i, t in enumerate(self.last) if t and now - t <= within]

    def packet(self, i: int, pcm: bytes, level: float, now: float) -> bytes | None:
        self.last[i], self.level[i] = now, level
        cur = self.current
        if cur is None or not self._open(cur, now) or \
                (cur != i and self.channels[i].get("priority") and not self.channels[cur].get("priority")):
            self.current = cur = i
        if cur != i:
            return None
        self._count(len(pcm) // 2, now)
        return pcm

    def title(self, now: float, idle: str) -> str:
        cur = self.current
        return self.channels[cur]["name"] if cur is not None and self._open(cur, now) else idle

    def _count(self, n: int, now: float) -> None:
        if self.t0 is None:
            self.t0 = now
        self.sent += n

    def filler(self, now: float, n: int = BATCH) -> bytes | None:
        if self.t0 is None:
            self.t0 = now
        if self.sent + n > (now - self.t0) * self.rate:
            return None
        if (now - self.t0) * self.rate - self.sent > 4 * self.rate:     # (a long stall: don't flood to catch up)
            self.t0, self.sent = now, 0
        self.sent += n
        return bytes(2 * n)


# --- the receiver ----------------------------------------------------------------------


class Listener:
    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.chunks: queue.Queue = queue.Queue(120)          # ~15 s: a listener this far behind is dropped
        self.dead = False


class Receiver:
    def __init__(self, bands: dict, airband: str | None = None, rtl_fm: str | None = None, gain: float = GAIN_DB,
                 ppm: int = 0, device: int = 0) -> None:
        self.bands = bands
        self.airband = airband or shutil.which("rtl_airband") or "/usr/local/bin/rtl_airband"
        self.rtl_fm = rtl_fm or shutil.which("rtl_fm") or "/usr/bin/rtl_fm"
        self.gain, self.ppm, self.device = gain, ppm, device
        self.lock = threading.RLock()
        self.spec: dict | None = None
        self.name = ""
        self.rate = RATE
        self.mixer: Mixer | None = None
        self.title = ""
        self.proc: subprocess.Popen | None = None
        self.gen = 0                                  # bumped on every change of what's being received
        self.listeners: list[Listener] = []
        self.idle_since = time.monotonic()
        self.error: str | None = None
        self.started = 0.0
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    # (what's received)

    def select(self, spec: dict) -> None:
        """Receive this (unless it already is)."""
        with self.lock:
            if spec == self.spec and self.proc is not None and self.proc.poll() is None:
                return
            self._stop()
            name, channels = spec_channels(spec, self.bands)
            self.spec, self.name, self.error = spec, name, None
            self.gen += 1
            for lis in self.listeners:               # (they were listening to something else)
                lis.dead = True
            self.listeners = []
            self.idle_since = self.started = time.monotonic()
            if spec.get("mode") == "wfm":
                self.rate, self.mixer, self.title = WFM_RATE, None, name
                # (said in full: this rtl_fm's "wbfm" alone gives 170 kHz audio, not 32 kHz)
                cmd = [self.rtl_fm, "-d", str(self.device), "-M", "wbfm", "-f", str(spec["freq"]), "-s", "170k",
                       "-r", str(WFM_RATE), "-E", "deemp", "-E", "dc", "-g", f"{self.gain:.1f}", "-p", str(self.ppm), "-"]
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                threading.Thread(target=self._wfm_run, args=(self.proc, self.gen), name="wfm", daemon=True).start()
            else:
                mode = spec.get("mode") or self.bands[spec["band"]].get("mode", "nfm")
                conf = airband_conf(channels, mode, spec.get("squelch", True), self.gain, self.ppm, self.device)
                path = Path(tempfile.gettempdir()) / "sleepradio-receiver.conf"
                path.write_text(conf)
                self.rate, self.mixer, self.title = RATE, Mixer(channels), name
                socks = []
                for i in range(len(channels)):
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", UDP_BASE + i))
                    socks.append(s)
                self.proc = subprocess.Popen([self.airband, "-F", "-e", "-c", str(path)], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE)
                threading.Thread(target=self._airband_run, args=(self.proc, socks, self.mixer, self.gen), name="airband",
                                 daemon=True).start()
            threading.Thread(target=self._stderr_run, args=(self.proc,), name="engine-log", daemon=True).start()
            log.info("receiving %s (%d channel%s)", name, len(channels), "" if len(channels) == 1 else "s")

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            time.sleep(0.3)                           # (the dongle needs a moment before it's opened again)

    def rest(self) -> None:
        with self.lock:
            if self.proc is not None:
                log.info("nobody listening: the dongle rests")
            self._stop()
            self.spec = None
            self.gen += 1

    def _stderr_run(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text and "\x1b" not in text:
                log.info("engine: %s", text[:200])
                low = text.lower()                    # (not "Tuner error set to 55 ppm": that's the correction)
                if "error set to" not in low and any(w in low for w in ("error", "failed", "no supported devices", "usb_claim")):
                    self.error = text[:200]
        code = proc.wait()
        if proc is self.proc:
            log.warning("the engine stopped (exit %s)", code)
            self.error = self.error or f"the receiver program stopped (exit {code})"

    # (audio in)

    def _send(self, gen: int, chunk: bytes) -> None:
        if gen != self.gen:
            return
        for lis in list(self.listeners):
            try:
                lis.chunks.put_nowait(chunk)
            except queue.Full:
                lis.dead = True

    def _airband_run(self, proc: subprocess.Popen, socks: list, mixer: Mixer, gen: int) -> None:
        index = {s: i for i, s in enumerate(socks)}
        try:
            while proc.poll() is None and gen == self.gen:
                ready, _, _ = select.select(socks, [], [], 0.05)
                now = time.monotonic()
                for s in ready:
                    data, _ = s.recvfrom(65536)
                    i = index[s]
                    if mixer.current in (None, i) or mixer.channels[i].get("priority") or not mixer._open(mixer.current, now):
                        pcm, level = to_pcm(data)
                    else:
                        pcm, level = b"", mixer.level[i]          # (not on air: only noted as active)
                    out = mixer.packet(i, pcm, level, now)
                    if out:
                        self._send(gen, out)
                fill = mixer.filler(now)
                if fill:
                    self._send(gen, fill)
                self.title = mixer.title(now, self.name)
        finally:
            for s in socks:
                s.close()

    def _wfm_run(self, proc: subprocess.Popen, gen: int) -> None:
        while gen == self.gen:
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break
            self._send(gen, data)

    # (listeners)

    def listen(self, spec: dict | None) -> Listener:
        with self.lock:
            if spec is not None:
                self.select(spec)
            elif self.spec is None:
                raise ValueError("nothing is being received: ask for ?band=NAME or ?freq=MHZ")
            lis = Listener(self.rate)
            self.listeners.append(lis)
            return lis

    def leave(self, lis: Listener) -> None:
        with self.lock:
            if lis in self.listeners:
                self.listeners.remove(lis)
            if not self.listeners:
                self.idle_since = time.monotonic()

    def _housekeeping(self) -> None:
        while True:
            time.sleep(5)
            with self.lock:
                self.listeners = [lis for lis in self.listeners if not lis.dead]
                idle = not self.listeners and self.proc is not None and time.monotonic() - self.idle_since > IDLE_S
            if idle:
                self.rest()

    def status(self) -> dict:
        now = time.monotonic()
        with self.lock:
            mixer, running = self.mixer, self.proc is not None and self.proc.poll() is None
            out = {"receiving": self.spec, "name": self.name if self.spec else None, "running": running,
                   "listeners": len(self.listeners), "title": self.title if self.spec else None, "error": self.error,
                   "rate": self.rate, "bands": {k: {"name": b["name"], "channels": len(b["channels"])} for k, b in self.bands.items()}}
            if running and mixer is not None and self.spec and self.spec.get("mode") != "wfm":
                on = mixer.current if mixer.current is not None and mixer._open(mixer.current, now) else None
                out["on_air"] = mixer.channels[on] if on is not None else None
                out["active"] = [mixer.channels[i] | {"level": round(mixer.level[i], 4)} for i in mixer.active(now)]
                out["heard"] = sorted(({**mixer.channels[i], "ago_s": round(now - t)} for i, t in enumerate(mixer.last) if t),
                                      key=lambda c: c["ago_s"])[:20]
            return out


# --- the web side ----------------------------------------------------------------------


def make_handler(receiver: Receiver):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "SleepRadioReceiver/1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _json(self, data, code: int = 200) -> None:
            body = json.dumps(data, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/status":
                return self._json(receiver.status())
            if url.path == "/bands":
                return self._json(receiver.bands)
            if url.path == "/":
                host = self.headers.get("Host") or f"localhost:{PORT}"
                return self._json({"what": "Sleep Radio receiver", "status": f"http://{host}/status",
                                   "listen": [f"http://{host}/audio?band={b}" for b in receiver.bands]
                                   + [f"http://{host}/audio?freq=145.5", f"http://{host}/audio?freq=95.0&mode=wfm"]})
            if url.path != "/audio":
                return self._json({"error": "not found"}, 404)
            try:
                spec = parse_spec(query, receiver.bands) if query else None
                lis = receiver.listen(spec)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            icy = self.headers.get("Icy-MetaData") == "1"
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-cache")
            if icy:
                self.send_header("icy-metaint", str(CHUNK_BYTES))
                self.send_header("icy-name", receiver.name)
            self.end_headers()
            try:
                # With ICY titles, a title block (or a 0) follows every CHUNK_BYTES of audio,
                # counted from the first byte -- the WAV header included.
                buf, said = wav_header(lis.rate), None
                if not icy:
                    self.wfile.write(buf)
                    buf = b""
                while not lis.dead:
                    try:
                        buf += lis.chunks.get(timeout=5)
                    except queue.Empty:
                        if receiver.error:
                            break
                        continue
                    if not icy:
                        self.wfile.write(buf)
                        buf = b""
                        continue
                    while len(buf) >= CHUNK_BYTES:
                        title = receiver.title
                        self.wfile.write(buf[:CHUNK_BYTES] + (b"\0" if title == said else icy_block(title)))
                        buf, said = buf[CHUNK_BYTES:], title
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                receiver.leave(lis)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/select":
                return self._json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                receiver.select(parse_spec({k: str(v) for k, v in body.items()}, receiver.bands))
            except (ValueError, TypeError, AttributeError) as e:
                return self._json({"error": str(e)}, 400)
            self._json(receiver.status())
    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sleep Radio receiver: an RTL-SDR dongle as a radio station.")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--bands", type=Path, default=Path("/etc/sleepradio-receiver/bands.json"), help="extra bands (JSON)")
    ap.add_argument("--gain", type=float, default=GAIN_DB, help="tuner gain in dB")
    ap.add_argument("--ppm", type=int, default=0,
                    help="the dongle's frequency error in parts per million (kal or rtl_test -p measures it): a cheap "
                         "dongle can be 50 ppm out, which at 145 MHz is 7 kHz -- most of a channel")
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--airband", help="path to rtl_airband")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    receiver = Receiver(load_bands(args.bands), airband=args.airband, gain=args.gain, ppm=args.ppm, device=args.device)
    httpd = ThreadingHTTPServer(("", args.port), make_handler(receiver))
    httpd.daemon_threads = True
    log.info("Sleep Radio receiver on port %d: bands %s", args.port, ", ".join(receiver.bands))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        receiver.rest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
