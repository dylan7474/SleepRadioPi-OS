"""Internet receivers as stations: OpenWebRX and KiwiSDR, without a browser.

Thousands of software radios are on the internet for anyone to tune: radio
amateurs' own, mostly. Each is a web page -- a waterfall you click on -- but
behind the page is a WebSocket that takes "tune here, this mode" and sends
back the audio, and that's all a radio with no screen needs.

A station whose address is a receiver's own link is played this way:

    http://HOST:8073/#freq=145500000,mod=nfm       OpenWebRX (and OpenWebRX+)
    http://HOST:8073/#freq=145500000,mod=nfm,sql=-70      ...with the squelch set by hand
    http://HOST:8073/?f=7150.00lsb                 KiwiSDR (kHz, then the mode)

These are the links the receivers' own pages make, so one copied from the
browser's address bar works as it is.

ReceiverStream looks like radio.RadioStream to the station: start(), read(),
buffered_s, ended, title, close(). The audio (12 kHz mono from both kinds)
is brought to the speaker's rate, and whenever the receiver has nothing to
send -- an FM channel with its squelch shut, a slow start -- silence goes out
in real time instead, so the station hears a quiet channel, not a dead one.

OpenWebRX: one dongle is on one band (a "profile") for everybody, so a
frequency outside the band it's on means asking for the profile that has it
-- which moves everyone else who's listening. A receiver can refuse that.
The squelch is automatic unless the link sets it: the level the receiver
reports with nobody transmitting, plus its own margin.

KiwiSDR: HF, with a few listener slots and daily time limits on public ones:
it says so when it won't have you, and that's passed on as why it stopped.
Be a good guest: these are someone's own radio.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import math
import queue
import re
import struct
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

RX_RATE = 12_000             # both kinds send 12 kHz mono
QUEUE_BLOCKS = 60
CONNECT_S = 10.0
QUIET_AFTER_S = 0.35         # no audio from the receiver this long: silence goes out instead
SQUELCH_LEARN_S = 2.5        # OpenWebRX: listen to the channel's level this long before setting the squelch
USER_AGENT = "SleepRadio"

OWRX_MODES = {"nfm", "am", "usb", "lsb", "cw", "sam", "wfm"}
OWRX_PASSBAND = {"nfm": (-4000, 4000), "am": (-4000, 4000), "sam": (-4000, 4000), "usb": (300, 3000), "lsb": (-3000, -300),
                 "cw": (700, 900), "wfm": (-75000, 75000)}
WF_ROWS = 80                # of a KiwiSDR's waterfall kept for the rig (about 6 s)
WF_ZOOM = 6                 # its zoom to begin with: 30 MHz / 2**6 = 469 kHz across
WF_SPEED = 3                # (of 4: about 13 rows a second)
KIWI_PASSBAND = {"am": (-4900, 4900), "amn": (-2500, 2500), "sam": (-4900, 4900), "usb": (300, 2700), "lsb": (-2700, -300),
                 "cw": (300, 700), "cwn": (470, 530), "nbfm": (-6000, 6000)}

# --- which receiver, tuned where -----------------------------------------------------------


def parse(url: str) -> dict | None:
    """A receiver's link -> {"kind": "owrx" | "kiwi", "base", "freq" (Hz), "mode", "sql"?}; None if it isn't one."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    base = f"{u.scheme}://{u.netloc}{u.path if u.path.endswith('/') else u.path.rsplit('/', 1)[0] + '/'}"
    frag = dict(p.split("=", 1) for p in u.fragment.split(",") if "=" in p)
    if "freq" in frag:                                       # OpenWebRX: #freq=145500000,mod=nfm,sql=-70
        try:
            spec = {"kind": "owrx", "base": base, "freq": int(float(frag["freq"])), "mode": (frag.get("mod") or "nfm").lower()}
            if "sql" in frag:
                spec["sql"] = float(frag["sql"])
        except ValueError:
            return None
        return spec if spec["mode"] in OWRX_MODES and spec["freq"] > 0 else None
    m = re.match(r"(\d+(?:\.\d+)?)([a-y]*)", (parse_qs(u.query).get("f") or [""])[0].lower())   # KiwiSDR: ?f=7150.00lsbz10
    if m and (m.group(2) or "am") in KIWI_PASSBAND:
        return {"kind": "kiwi", "base": base, "freq": int(round(float(m.group(1)) * 1000)), "mode": m.group(2) or "am"}
    return None


def is_receiver(url: str) -> bool:
    return parse(url) is not None


def said(spec: dict) -> str:
    """145.500 MHz FM / 7150 kHz LSB: what the radio's page shows."""
    mode = {"nfm": "FM", "nbfm": "FM", "wfm": "FM"}.get(spec["mode"], spec["mode"].upper())
    hz = spec["freq"]
    return (f"{hz / 1e6:.4f}".rstrip("0").rstrip(".") + " MHz " if hz >= 30_000_000 else f"{hz / 1000:g} kHz ") + mode


# --- audio -----------------------------------------------------------------------------------

_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
         143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
         1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132,
         7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767]
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


class Adpcm:
    """OpenWebRX's audio: IMA ADPCM, low nibble first, with "SYNC" + the
    decoder's state (step index, predictor: int16 each) every 1000 bytes."""

    def __init__(self) -> None:
        self.idx = self.pred = self.phase = self.sync = self.left = 0
        self.head = b""

    def _nibble(self, n: int) -> int:
        step = _STEP[self.idx]
        d = step >> 3
        if n & 1:
            d += step >> 2
        if n & 2:
            d += step >> 1
        if n & 4:
            d += step
        self.pred = max(-32768, min(32767, self.pred - d if n & 8 else self.pred + d))
        self.idx = max(0, min(88, self.idx + _INDEX[n & 7]))
        return self.pred

    def decode(self, data: bytes) -> np.ndarray:
        out = []
        for b in data:
            if self.phase == 0:
                self.sync = self.sync + 1 if b == b"SYNC"[self.sync] else (1 if b == 0x53 else 0)
                if self.sync == 4:
                    self.sync, self.phase, self.head = 0, 1, b""
            elif self.phase == 1:
                self.head += bytes([b])
                if len(self.head) == 4:
                    self.idx, self.pred = struct.unpack("<hh", self.head)
                    self.idx = max(0, min(88, self.idx))
                    self.phase, self.left = 2, 1000
            else:
                out.append(self._nibble(b & 0x0F))
                out.append(self._nibble(b >> 4))
                self.left -= 1
                if self.left == 0:
                    self.phase = 0
        return np.array(out, dtype=np.int16)


class Resampler:
    """Mono at the receiver's rate -> the speaker's rate and channels (straight
    lines between samples: it's speech through a radio)."""

    def __init__(self, rate_in: float, rate_out: int = pcm.SAMPLE_RATE, channels: int = pcm.CHANNELS) -> None:
        self.step = rate_in / rate_out
        self.channels = channels
        self.pos = 0.0                    # where the next output sample falls, in input samples past `last`
        self.last = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if not len(x):
            return np.zeros((0, self.channels), dtype=np.int16)
        src = np.concatenate(([self.last], x.astype(np.float32)))
        n = int(math.floor((len(x) - self.pos) / self.step)) + 1 if self.pos <= len(x) else 0
        at = self.pos + self.step * np.arange(max(0, n))
        at = at[at <= len(x)]
        y = np.interp(at, np.arange(len(src)), src)
        self.pos = (at[-1] + self.step if len(at) else self.pos) - len(x)
        self.last = float(x[-1])
        return np.repeat(np.clip(y, -32768, 32767).astype(np.int16)[:, None], self.channels, axis=1)


# --- the stream --------------------------------------------------------------------------------


def _ws_url(base: str, path: str) -> str:
    return ("wss://" if base.startswith("https://") else "ws://") + base.split("://", 1)[1].rstrip("/") + path


def _connect(url: str):
    from websockets.sync.client import connect        # (python-websockets: in the image; a desktop may lack it)
    return connect(url, open_timeout=CONNECT_S, max_size=None, user_agent_header=USER_AGENT)


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=CONNECT_S) as r:
        return r.read(2_000_000).decode(errors="replace")


class ReceiverStream:
    """A receiver, tuned, as the station sees a stream (see radio.RadioStream)."""

    def __init__(self, url: str, connect: Callable = _connect, get_text: Callable = _get_text,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = url
        self.spec = parse(url)
        self.title: str | None = None
        self.ended: str | None = None
        self.total_bytes = 0
        self._connect, self._get_text, self._clock = connect, get_text, clock
        self._closed = threading.Event()
        self._blocks: queue.Queue = queue.Queue(QUEUE_BLOCKS)
        self._ws = None
        self._pending = np.zeros((0, pcm.CHANNELS), dtype=np.int16)
        self._lock = threading.Lock()
        self._last_audio = 0.0
        self._t0: float | None = None
        self._sent = 0                    # frames queued since _t0 (audio and silence): kept in step with the clock
        # (for the rig on the desktop: where it's tuned, how strong, and the receiver's own waterfall)
        self.dbm: float | None = None
        self.name = ""
        self._wf = None
        self._wf_rows: collections.deque = collections.deque(maxlen=WF_ROWS)    # (number, lo Hz, hi Hz, a byte per bin: dBm + 255)
        self._wf_n = 0
        self._wf_view: tuple[int, float] | None = None       # (zoom, centre kHz) asked for
        self._wf_asked = 0.0
        self._wf_last = 0.0
        self._cfg: dict = {}              # an OpenWebRX's settings as it sends them (its band's centre and width, ...)
        self._profiles: list = []         # ...its bands
        self._asks: list[dict] = []       # ...and what the rig has asked of it, for its loop to do

    # (what the station uses)

    def start(self) -> None:
        threading.Thread(target=self._run, name="receiver", daemon=True).start()
        threading.Thread(target=self._pace, name="receiver-pace", daemon=True).start()

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            return self._blocks.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def buffered_s(self) -> float:
        return self._blocks.qsize() * pcm.CHUNK_FRAMES / pcm.SAMPLE_RATE

    def close(self) -> None:
        self._closed.set()
        for name in ("_ws", "_wf"):
            ws = getattr(self, name)
            setattr(self, name, None)
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    # (what the rig uses: one connection to the receiver serves the listening and the looking)

    def rig(self, since: int = 0) -> dict:
        """Where it's tuned and what its waterfall shows: the rows after number `since`
        that belong to the present view (each a byte per bin from lo to hi: dBm + 255)."""
        spec = self.spec or {}
        self._wf_asked = self._clock()
        rows = list(self._wf_rows)
        lo, hi = (rows[-1][1], rows[-1][2]) if rows else (None, None)
        out = {"kind": spec.get("kind"), "base": spec.get("base"), "freq": spec.get("freq"), "mode": spec.get("mode"),
               "name": self.name, "title": self.title, "dbm": self.dbm, "ended": self.ended, "tunes": spec.get("kind") in ("kiwi", "owrx"),
               "lo": lo, "hi": hi, "n": self._wf_n,
               "rows": [[n, base64.b64encode(row).decode()] for n, a, b, row in rows if n > since and (a, b) == (lo, hi)]}
        if spec.get("kind") == "owrx":
            # Its rows are as it sent them (the page works them out: "adpcm" is IMA ADPCM from a fresh
            # start, ten values to throw away, then hundredths of a dB; "none" is float32 dB), and it has bands.
            cfg = self._cfg
            out.update(codec=cfg.get("fft_compression", "adpcm"), band=f"{cfg.get('sdr_id')}|{cfg.get('profile_id')}",
                       bands=[{"id": p.get("id"), "name": str(p.get("name", ""))[:200]} for p in self._profiles if isinstance(p, dict)])
        return out

    def tune(self, freq: int | None = None, mode: str | None = None, zoom: int | None = None, centre: int | None = None,
             profile: str | None = None) -> None:
        """Retune where it is, on the connection it has: the frequency (Hz) and mode it
        listens on; a KiwiSDR's waterfall zoom and centre (Hz); an OpenWebRX's band
        (profile: it moves everyone listening to that receiver). ValueError says why not."""
        spec, ws = self.spec, self._ws
        if spec is not None and spec["kind"] == "owrx":
            ask: dict = {}
            if profile is not None:
                if not any(isinstance(p, dict) and p.get("id") == profile for p in self._profiles):
                    raise ValueError("that receiver has no such band")
                ask["profile"] = profile
            if mode is not None:
                if mode.lower() not in OWRX_MODES:
                    raise ValueError(f"its modes are {', '.join(sorted(OWRX_MODES))}")
                ask["mode"] = mode.lower()
            if freq is not None:
                cfg = self._cfg
                if profile is None and ("center_freq" not in cfg or abs(freq - cfg["center_freq"]) > cfg.get("samp_rate", 0) / 2 * 0.98):
                    raise ValueError("that's outside the band it's on: choose another band")
                ask["freq"] = int(freq)
            if ask:
                if ws is None:
                    raise ValueError("the receiver isn't connected yet")
                self._asks.append(ask)
            return
        if spec is None or spec["kind"] != "kiwi":
            raise ValueError("this kind of receiver is tuned by choosing it again")
        if freq is not None or mode is not None:
            mode = (mode or spec["mode"]).lower()
            freq = int(spec["freq"] if freq is None else freq)
            if mode not in KIWI_PASSBAND:
                raise ValueError(f"its modes are {', '.join(KIWI_PASSBAND)}")
            if not 0 < freq <= 32_000_000:
                raise ValueError("a KiwiSDR tunes from 0 to 30 MHz")
            if ws is None:
                raise ValueError("the receiver isn't connected yet")
            lo, hi = KIWI_PASSBAND[mode]
            ws.send(f"SET mod={mode} low_cut={lo} high_cut={hi} freq={freq / 1000:.3f}")
            spec["freq"], spec["mode"] = freq, mode
            self.title = said(spec) + (f" · {self.name[:60]}" if self.name else "")
        if zoom is not None or centre is not None:
            z = max(0, min(14, int(self._wf_view[0] if zoom is None and self._wf_view else zoom or 0)))
            cf = (centre if centre is not None else spec["freq"]) / 1000
            self._wf_view = (z, cf)
            wf = self._wf
            if wf is not None:
                wf.send(f"SET zoom={z} cf={cf:.3f}")

    def _kiwi_waterfall(self, spec: dict, stamp: int) -> None:
        """The KiwiSDR's waterfall, on its own socket beside the sound's (the same stamp pairs
        them). Some have fewer waterfalls than listeners: then there's simply none."""
        bandwidth, zoom_max, started, keep = 30_000_000, 14, False, 0.0
        try:
            wf = self._wf = self._connect(_ws_url(spec["base"], f"/kiwi/{stamp}/W/F"))
            wf.send("SET auth t=kiwi p=")
            while not self._closed.is_set() and self.ended is None:
                now = self._clock()
                if now - keep > 4:
                    wf.send("SET keepalive")
                    keep = now
                m = self._recv(wf, 1.0)
                if m is None:
                    continue
                if isinstance(m, str):
                    m = m.encode()
                if m[:3] == b"MSG":
                    text = m[4:].decode(errors="replace")
                    for key, word in (("bandwidth=", "bandwidth"), ("zoom_max=", "zoom_max")):
                        if key in text:
                            try:
                                value = int(float(text.split(key)[1].split()[0]))
                                bandwidth, zoom_max = (value, zoom_max) if word == "bandwidth" else (bandwidth, value)
                            except ValueError:
                                pass
                    if not started:
                        z, cf = self._wf_view or (WF_ZOOM, spec["freq"] / 1000)
                        self._wf_view = (z, cf)
                        for c in (f"SET zoom={z} cf={cf:.3f}", "SET maxdb=0 mindb=-100", f"SET wf_speed={WF_SPEED}", "SET wf_comp=0",
                                  "SET ident_user=SleepRadio"):
                            wf.send(c)
                        started = True
                elif m[:3] == b"W/F" and len(m) > 16:
                    if self._clock() - self._wf_asked > 15:       # (nobody is looking: not kept)
                        continue
                    x_bin, flags = struct.unpack("<II", m[4:12])
                    zoom, bins = flags & 0xFFFF, len(m) - 16
                    lo = x_bin * bandwidth / (bins << zoom_max)
                    self._wf_n += 1
                    self._wf_rows.append((self._wf_n, int(lo), int(lo + bandwidth / (1 << zoom)), bytes(m[16:])))
        except Exception as e:
            if not self._closed.is_set():
                log.info("receiver: no waterfall: %s", _why(e))
        finally:
            wf, self._wf = self._wf, None
            if wf is not None:
                try:
                    wf.close()
                except Exception:
                    pass

    def _end(self, why: str) -> None:
        if self.ended is None and not self._closed.is_set():
            self.ended = why
            log.info("receiver: ended: %s", why)

    # (audio out: the receiver's when it sends some, silence in real time when it doesn't)

    def _put(self, frames: np.ndarray) -> None:
        with self._lock:
            self._pending = np.concatenate([self._pending, frames]) if len(self._pending) else frames
            while len(self._pending) >= pcm.CHUNK_FRAMES:
                block, self._pending = self._pending[:pcm.CHUNK_FRAMES], self._pending[pcm.CHUNK_FRAMES:]
                try:
                    self._blocks.put_nowait(block)
                except queue.Full:                 # (the station isn't taking it: paused elsewhere. Live, so drop the oldest)
                    try:
                        self._blocks.get_nowait()
                        self._blocks.put_nowait(block)
                    except (queue.Empty, queue.Full):
                        pass
            self._sent += len(frames)

    def _audio(self, frames: np.ndarray) -> None:
        if len(frames):
            now = self._clock()
            if self._t0 is None:
                self._t0 = now
            self._last_audio = now
            self._put(frames)

    def fill(self, now: float) -> int:
        """Queue silence if the receiver has sent nothing lately and the stream
        is behind the clock. Returns the frames added."""
        if self._t0 is None:
            self._t0 = now
        if now - self._last_audio < QUIET_AFTER_S:
            return 0
        due = int((now - self._t0) * pcm.SAMPLE_RATE) - self._sent
        if due > 4 * pcm.SAMPLE_RATE:              # (a long stall: start afresh rather than flood)
            self._t0, self._sent, due = now, 0, pcm.CHUNK_FRAMES
        if due < pcm.CHUNK_FRAMES // 4:
            return 0
        self._put(np.zeros((due, pcm.CHANNELS), dtype=np.int16))
        return due

    def _pace(self) -> None:
        while not self._closed.is_set() and self.ended is None:
            self.fill(self._clock())
            self._closed.wait(0.05)

    # (the receiver's side)

    def _run(self) -> None:
        spec = self.spec
        try:
            if spec is None:
                raise ValueError("that isn't a receiver's address")
            (self._owrx if spec["kind"] == "owrx" else self._kiwi)(spec)
        except Exception as e:
            if not self._closed.is_set():
                self._end(_why(e))
        finally:
            ws, self._ws = self._ws, None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _recv(self, ws, timeout: float = 2.0):
        try:
            return ws.recv(timeout=timeout)
        except TimeoutError:
            return None

    def _owrx(self, spec: dict) -> None:
        status = {}
        try:
            status = json.loads(self._get_text(spec["base"] + "status.json"))
        except Exception:
            pass                                    # (older ones have none: it's only for finding the right band)
        name = self.name = (status.get("receiver") or {}).get("name") or ""
        self.title = said(spec) + (f" · {name[:60]}" if name else "")
        ws = self._ws = self._connect(_ws_url(spec["base"], "/ws/"))
        ws.send("SERVER DE CLIENT client=sleepradio type=receiver")
        ws.send(json.dumps({"type": "connectionproperties", "params": {"output_rate": RX_RATE, "hd_output_rate": 48000}}))
        cfg = self._cfg = {}
        profiles: list = []
        adpcm, resample = Adpcm(), Resampler(RX_RATE)
        tuned = asked_profile = chosen = False
        margin = 10.0
        levels: list[float] = []
        learn_from: float | None = None
        squelch_set = "sql" in spec or spec["mode"] != "nfm"      # (narrow FM gets an automatic squelch; the rest are left open)
        deadline = self._clock() + 25.0             # (a sleeping dongle takes ~10 s to start sending)

        def tune() -> None:
            lo, hi = OWRX_PASSBAND[spec["mode"]]
            ws.send(json.dumps({"type": "dspcontrol", "action": "start"}))
            ws.send(json.dumps({"type": "dspcontrol", "params": {
                "low_cut": lo, "high_cut": hi, "offset_freq": spec["freq"] - cfg["center_freq"], "mod": spec["mode"],
                "squelch_level": spec.get("sql", -150), "secondary_mod": False}}))
            self.title = said(spec) + (f" · {name[:60]}" if name else "")

        while not self._closed.is_set():
            m = self._recv(ws)
            now = self._clock()
            while self._asks:                       # (from the rig: another band, or another frequency or mode in this one)
                ask = self._asks.pop(0)
                if "mode" in ask:
                    spec["mode"] = ask["mode"]
                if "profile" in ask:
                    log.info("receiver: asking for the band %s", ask["profile"])
                    ws.send(json.dumps({"type": "selectprofile", "params": {"profile": ask["profile"]}}))
                    for k in ("center_freq", "samp_rate", "start_freq", "start_mod"):
                        cfg.pop(k, None)
                    spec["freq"] = ask.get("freq", 0)             # (0: wherever that band starts)
                    tuned, asked_profile, chosen, deadline = False, True, True, now + 25.0
                    self._wf_rows.clear()
                else:
                    spec["freq"] = ask.get("freq", spec["freq"])
                    if tuned:
                        tune()
                # narrow FM gets a squelch, worked out afresh where it now is; the other modes are left open
                squelch_set, levels, learn_from = "sql" in spec or spec["mode"] != "nfm", [], None
            if m is None:
                if not tuned and now > deadline:
                    raise ValueError("the receiver didn't answer")
                continue
            if isinstance(m, bytes):
                if m[:1] == b"\x01" and "center_freq" in cfg and "samp_rate" in cfg:        # its waterfall, for the rig
                    if now - self._wf_asked < 15 and now - self._wf_last >= 0.15:           # (someone's looking; six a second is plenty)
                        self._wf_last = now
                        self._wf_n += 1
                        half = cfg["samp_rate"] / 2
                        self._wf_rows.append((self._wf_n, int(cfg["center_freq"] - half), int(cfg["center_freq"] + half), bytes(m[1:])))
                    continue
                if tuned and m[:1] == b"\x02":
                    x = adpcm.decode(m[1:]) if cfg.get("audio_compression", "adpcm") == "adpcm" else np.frombuffer(
                        m[1:1 + (len(m) - 1) // 2 * 2], dtype="<i2")
                    if squelch_set:                 # (while the squelch is being worked out, the channel's noise isn't played)
                        self._audio(resample.process(x))
                continue
            if not m.startswith("{"):
                continue
            try:
                j = json.loads(m)
            except ValueError:
                continue
            kind, value = j.get("type"), j.get("value")
            if kind == "config" and isinstance(value, dict):
                cfg.update(value)
                margin = float(cfg.get("squelch_auto_margin", margin) or margin)
            elif kind == "profiles" and isinstance(value, list):
                profiles = self._profiles = value
            elif kind == "smeter" and tuned and not squelch_set and value:
                if learn_from is None:
                    learn_from = now
                levels.append(10 * math.log10(max(float(value), 1e-30)))
                if now - learn_from >= SQUELCH_LEARN_S and len(levels) >= 3:
                    level = float(np.percentile(levels, 20)) + margin      # (the quieter readings: the channel with nobody on it)
                    ws.send(json.dumps({"type": "dspcontrol", "params": {"squelch_level": level}}))
                    squelch_set = True
                    log.info("receiver: squelch at %.0f dB", level)
            elif kind in ("sdr_error", "demodulator_error") and value:
                raise ValueError(f"the receiver said: {str(value)[:120]}")
            elif kind == "backoff":
                raise ValueError("the receiver is full")
            if not tuned and "center_freq" in cfg and "samp_rate" in cfg:
                half = cfg["samp_rate"] / 2
                if chosen and abs(spec["freq"] - cfg["center_freq"]) > half * 0.98:          # a band chosen on the rig: where it starts
                    start = cfg.get("start_freq")
                    spec["freq"] = int(start) if start and abs(start - cfg["center_freq"]) <= half * 0.98 else int(cfg["center_freq"] + half / 2)
                    if cfg.get("start_mod") in OWRX_MODES and cfg["start_mod"] != "wfm":
                        spec["mode"] = cfg["start_mod"]
                    squelch_set = "sql" in spec or spec["mode"] != "nfm"
                if abs(spec["freq"] - cfg["center_freq"]) <= half * 0.98:
                    tune()
                    tuned = True
                elif not asked_profile and profiles:
                    want = _owrx_profile(spec["freq"], status, profiles)
                    if want is None:
                        raise ValueError(f"that receiver has no band with {said(spec)} in it")
                    log.info("receiver: asking for the band %s", want)
                    ws.send(json.dumps({"type": "selectprofile", "params": {"profile": want}}))
                    asked_profile = True
                    for k in ("center_freq", "samp_rate"):
                        cfg.pop(k, None)
                    deadline = now + 25.0
                elif asked_profile and now > deadline:
                    raise ValueError("the receiver wouldn't change band (someone else may be using it)")
        ws.close()

    def _kiwi(self, spec: dict) -> None:
        try:
            st = dict(line.split("=", 1) for line in self._get_text(spec["base"] + "status").splitlines() if "=" in line)
        except Exception:
            st = {}
        name = self.name = st.get("name", "")
        self.title = said(spec) + (f" · {name[:60]}" if name else "")
        stamp = int(time.time())
        ws = self._ws = self._connect(_ws_url(spec["base"], f"/kiwi/{stamp}/SND"))
        ws.send("SET auth t=kiwi p=")
        started, resample, keep = False, None, 0.0
        deadline = self._clock() + 20.0
        while not self._closed.is_set():
            now = self._clock()
            if now - keep > 4:
                ws.send("SET keepalive")
                keep = now
            m = self._recv(ws)
            if m is None:
                if not started and now > deadline:
                    raise ValueError("the receiver didn't answer")
                continue
            if isinstance(m, str):
                m = m.encode()
            tag = m[:3]
            if tag == b"MSG":
                text = m[4:].decode(errors="replace")
                for why, words in (("too_busy=", "the receiver is full: all its listener slots are taken"),
                                   ("badp=1", "the receiver wants a password"), ("down=", "the receiver is switched off for now"),
                                   ("inactivity_timeout=", "the receiver's time limit was reached"),
                                   ("ip_limit=", "the receiver's daily time limit for one listener was reached")):
                    if why in text and not (why == "too_busy=" and "too_busy=0" in text):
                        raise ValueError(words)
                if not started and ("audio_rate=" in text or "sample_rate=" in text):
                    key = "audio_rate=" if "audio_rate=" in text else "sample_rate="
                    rate = float(text.split(key)[1].split()[0])
                    lo, hi = KIWI_PASSBAND[spec["mode"]]
                    for c in (f"SET AR OK in={int(round(rate))} out=44100", "SET squelch=0 max=0", "SET genattn=0", "SET gen=0 mix=-1",
                              "SET ident_user=SleepRadio",
                              f"SET mod={spec['mode']} low_cut={lo} high_cut={hi} freq={spec['freq'] / 1000:.3f}",
                              "SET agc=1 hang=0 thresh=-100 slope=6 decay=1000 manGain=50", "SET compression=0", "SET keepalive"):
                        ws.send(c)
                    started, resample = True, Resampler(rate)
                    threading.Thread(target=self._kiwi_waterfall, args=(spec, stamp), name="receiver-waterfall", daemon=True).start()
            elif tag == b"SND" and started and len(m) > 10:
                self.dbm = 0.1 * struct.unpack(">H", m[8:10])[0] - 127
                self._audio(resample.process(np.frombuffer(m[10:10 + (len(m) - 10) // 2 * 2], dtype=">i2")))
        ws.close()


def _owrx_profile(freq: int, status: dict, profiles: list) -> str | None:
    """The id of a band (profile) of this OpenWebRX with freq in it, nearest its
    centre; from status.json's bands, matched to the ids by name."""
    best = None
    for sdr in status.get("sdrs") or []:
        for p in sdr.get("profiles") or []:
            try:
                off = abs(freq - p["center_freq"])
                if off <= p["sample_rate"] / 2 * 0.9 and (best is None or off < best[0]):
                    best = (off, f"{sdr.get('name', '')} {p.get('name', '')}".strip())
            except (KeyError, TypeError):
                continue
    if best is None:
        return None
    for p in profiles:
        if isinstance(p, dict) and str(p.get("name", "")).strip() == best[1]:
            return p.get("id")
    return None


def _why(e: Exception) -> str:
    text = str(e) or type(e).__name__
    if isinstance(e, ValueError):
        return text
    if isinstance(e, ModuleNotFoundError):
        return "this radio's software can't play receivers (python-websockets is missing)"
    if isinstance(e, (TimeoutError, OSError)):
        return f"the receiver couldn't be reached ({text[:80]})"
    return f"the receiver's connection broke ({text[:80]})"
