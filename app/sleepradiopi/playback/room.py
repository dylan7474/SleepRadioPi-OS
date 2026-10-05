"""A room: radio amateurs' digital voice, listened to as a station.

Yaesu "System Fusion" radios talk to each other across the internet through
reflectors -- rooms -- that anyone licensed can join (the YSF network a
Pi-Star hotspot uses; many of Yaesu's own WIRES-X rooms are bridged onto
it). A station whose address is a room's

    ysf://HOST:PORT/NAME

(or fcs://FCS00290/NAME for a room on the FCS network, where Yaesu's
WIRES-X rooms are mostly bridged: the same radio frames in another
envelope, on fcs002.xreflector.net and its like)

joins it and plays whoever speaks, with their callsign as the "now playing"
title. It only listens: nothing is ever sent but "I'm here" every few
seconds, under the callsign the radio has been given (a room shows who is
connected, and wants a real one). Rooms are quiet most of the time: silence
goes out meanwhile, so the radio stays tuned in.

The speech is in the AMBE+2 codec, which this software does not contain.
A separate decoder -- mbelib, built by scripts/build-voice-decoder.sh and
put on the radio as DECODER -- is loaded if it's there. Without it a room
still shows who is talking; it just can't be heard. (Why it's separate is
in the README: the codec is patented, and what's covered where is for
whoever installs it to weigh.)

A frame from the reflector is "YSFD", three callsigns, a counter, then a
Fusion radio frame: 5 bytes of sync, 25 of header (FICH), and five blocks
of 40 data bits + 104 voice bits. In the usual "DN" mode a voice block,
de-interleaved and un-whitened, is 27 bits each sent three times and 22
sent once: the 49 bits of one 20 ms AMBE+2 frame. That the triples agree is
how a voice frame is told from a header or a data frame.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import math
import re
import socket
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.playback import dmr_ids
from sleepradiopi.playback.receiver import ReceiverStream, Resampler

log = logging.getLogger(__name__)

DECODER = Path.home() / "decoders" / "libmbe.so"
CALLSIGN: str | None = None      # the radio's (main.py sets it from the settings; the web side when it's changed)
PORT = 42000
FCS_PORT = 62500
FCS_POLL_S = 0.8                 # (the FCS network wants to hear from you this often)
POLL_S = 5.0                     # "I'm here", this often
GONE_S = 60.0                    # no answer this long: the room has gone
STATUS_S = 60.0                  # how many are connected: asked this often
OVER_S = 1.5                     # nothing from the talker this long: the over has ended
VOICE_RATE = 8000
LEVEL = 100                      # how loud rooms are, % (the "room level" setting: main.py and the web side set it)
MIN_LEVEL, MAX_LEVEL = 25, 400
TARGET = 0.15                    # where the loud parts of a voice are put (rms of full scale, over a tenth of a second), at 100%
MIN_GAIN, MAX_GAIN = 0.25, 12.0  # how far a talker may be turned down, and up, to get there (the room level is on top of this)
OVER_TARGET = 1.4                # no tenth of a second leaves louder than this many times the target
FALL_S = 3.0                     # a talker who drops their voice is followed down over this long
HEARD = 20                       # overs remembered, for "last heard"

_WHITEN20 = bytes([0x93, 0xD7, 0x51, 0x21, 0x9C, 0x2F, 0x6C, 0xD0, 0xEF, 0x0F, 0xF8, 0x3D, 0xF1, 0x73, 0x20, 0x94, 0xED, 0x1E, 0x7C, 0xD8])
_SPREAD = np.array([(i % 5) * 40 + (i // 5) * 2 for i in range(100)])     # where each coded pair sits among 200 bits
_WHITEN = np.unpackbits(np.frombuffer(bytes([0x93, 0xD7, 0x51, 0x21, 0x9C, 0x2F, 0x6C, 0xD0, 0xEF, 0x0F, 0xF8, 0x3D, 0xF1]), dtype=np.uint8))
_ORDER = np.array([(i % 26) * 4 + i // 26 for i in range(104)])


def parse(url: str) -> dict | None:
    """ysf://HOST[:PORT][/NAME] or fcs://FCS00290[/NAME] -> {"net", "host", "port", "name"} (and, for FCS,
    "id": the room's eight characters); None if it isn't a room's address."""
    try:
        u = urlparse(url)
        name = unquote(u.path.strip("/"))[:40]
        if u.scheme == "ysf" and u.hostname:
            return {"net": "ysf", "host": u.hostname, "port": u.port or PORT, "name": name or u.hostname}
        if u.scheme == "fcs" and re.fullmatch(r"fcs\d{5}", u.hostname or ""):
            rid = u.hostname.upper()
            return {"net": "fcs", "host": f"{rid[:6].lower()}.xreflector.net", "port": FCS_PORT, "name": name or rid, "id": rid}
    except ValueError:
        pass
    return None


def is_room(url: str) -> bool:
    return parse(url) is not None


def link(host: str, port: int, name: str) -> str:
    return f"ysf://{host}:{int(port)}/{quote(name.strip(), safe='')}"


def fcs_link(room_id: str, name: str) -> str:
    return f"fcs://{room_id.upper()}/{quote(name.strip(), safe='')}"


def clean_callsign(text) -> str:
    """A callsign as a room wants it (M8ODJ, or M8ODJ-1 / M8ODJ/P); "" clears it. ValueError if it isn't one."""
    call = str(text or "").strip().upper()
    if call and not re.fullmatch(r"(?=.*\d)(?=.*[A-Z])[A-Z0-9]{3,8}([-/][A-Z0-9]{1,3})?", call):
        raise ValueError("a callsign is letters and digits, like M0ABC")
    if len(call) > 10:
        raise ValueError("a callsign is 10 characters at most")
    return call


def voice(payload: bytes) -> tuple[list[np.ndarray], float]:
    """One 120-byte Fusion frame -> its five 49-bit voice frames, and how well the
    bits sent three times agree (1.0: all of them -- it's voice in DN mode)."""
    body = np.unpackbits(np.frombuffer(payload[:120], dtype=np.uint8))[(5 + 25) * 8:]
    out, agree = [], 0
    for j in range(5):
        block = body[j * 144 + 40:j * 144 + 144][_ORDER] ^ _WHITEN
        sums = block[:81].reshape(27, 3).sum(axis=1)
        agree += int((sums % 3 == 0).sum())
        out.append(np.concatenate([(sums >= 2).astype(np.uint8), block[81:103]]))
    return out, agree / 135


def _crc_ok(data: bytes) -> bool:
    """CRC-16 (CCITT, from zero, inverted) over all but the last two bytes, which are it."""
    crc = 0
    for b in data[:-2]:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return (crc ^ 0xFFFF) == int.from_bytes(data[-2:], "big")


def _unfold(bits200: np.ndarray) -> np.ndarray | None:
    """200 bits off the air -> the 100 they carry. They're spread out, and each one was
    sent as a pair (a rate-1/2 convolutional code: g1 = d + d3 + d4, g2 = d + d1 + d2 + d4).
    What a reflector passes on has already been put right by the radio that received it,
    so the first of each pair gives the bit and the second only checks it: None if any
    pair disagrees (the caller's CRC catches what that doesn't)."""
    g1, g2 = bits200[_SPREAD], bits200[_SPREAD + 1]
    out = np.zeros(100, dtype=np.uint8)
    d1 = d2 = d3 = d4 = 0
    for i in range(100):
        d = int(g1[i]) ^ d3 ^ d4
        if d ^ d1 ^ d2 ^ d4 != int(g2[i]):
            return None
        out[i] = d
        d1, d2, d3, d4 = d, d1, d2, d3
    return out


def fich(payload: bytes) -> dict | None:
    """A frame's header: {"fi": 0 header | 1 voice or data | 2 the end, "fn": its number in the
    cycle, "dt": 0 | 1 data | 2 voice (DN) | 3 voice (VW)}; None if it can't be read."""
    bits = _unfold(np.unpackbits(np.frombuffer(payload[5:30], dtype=np.uint8)))
    if bits is None:
        return None
    # four Golay (24,12) words: the first twelve bits of each are the data
    data = np.packbits(np.concatenate([bits[k * 24:k * 24 + 12] for k in range(4)])).tobytes()
    if not _crc_ok(data):
        return None
    return {"fi": data[0] >> 6, "fn": (data[1] >> 3) & 7, "ft": data[1] & 7, "dt": data[2] & 3}


def dch(payload: bytes) -> bytes | None:
    """The ten bytes of data riding with a DN voice frame -- frame 0 of a cycle has who it's
    to, frame 1 who it's from -- or None if they can't be read."""
    body = np.unpackbits(np.frombuffer(payload[30:120], dtype=np.uint8))
    bits = _unfold(np.concatenate([body[j * 144:j * 144 + 40] for j in range(5)]))
    if bits is None:
        return None
    data = np.packbits(bits[:96]).tobytes()
    return bytes(b ^ w for b, w in zip(data[:10], _WHITEN20)) if _crc_ok(data) else None


def is_silence(bits: np.ndarray) -> bool:
    """The codec's own "nothing is being said" frame (its pitch field is 124 or 125): a key
    held with nobody speaking. Played, it's only hiss."""
    b0 = 0
    for b in (*bits[0:6], bits[48]):
        b0 = b0 * 2 + int(b)
    return b0 in (124, 125)


class Decoder:
    """mbelib, if it has been put on the radio (see the module's words): 49 bits -> 20 ms of speech."""

    def __init__(self, path: Path = DECODER) -> None:
        self.lib = ctypes.CDLL(str(path))
        self.out = (ctypes.c_short * 160)()
        self.errs, self.errs2, self.err_str = ctypes.c_int(), ctypes.c_int(), ctypes.create_string_buffer(256)
        self.parms = [ctypes.create_string_buffer(65536) for _ in range(3)]      # (mbe_parms: more room than it needs)
        self.reset()

    def reset(self) -> None:
        self.lib.mbe_initMbeParms(*self.parms)

    def decode(self, bits: np.ndarray) -> np.ndarray:
        buf = (ctypes.c_char * 49).from_buffer_copy(bytes(bits[:49].tolist()))
        self.lib.mbe_processAmbe2450Data(self.out, ctypes.byref(self.errs), ctypes.byref(self.errs2), self.err_str, buf,
                                         self.parms[0], self.parms[1], self.parms[2], 3)
        return np.frombuffer(self.out, dtype=np.int16).copy()


def load_decoder(path: Path | None = None) -> Decoder | None:
    path = DECODER if path is None else path
    if not path.is_file():
        return None
    try:
        return Decoder(path)
    except (OSError, AttributeError) as e:
        log.warning("room: the voice decoder at %s can't be used: %s", path, e)
        return None


class Shaper:
    """A room's speech at the level it's played. Talkers come out of the decoder at very
    different levels (one a little under the music, the next a seventh of it), so each
    is brought to the same place: the level of the loud parts of their voice is followed
    (up quickly, down slowly, the gaps between words left out of it) and put at TARGET
    times the room level setting -- a little under the music, since speech at music's
    average level sounds much louder. Nothing leaves far over that, and a soft limit
    takes what's left. It is NOT the station's leveller, which works on the average over
    seconds: that turned speech up to music's level and clipped it."""

    def __init__(self, rate: int = VOICE_RATE) -> None:
        self.rate = rate
        self.reset()

    def reset(self) -> None:
        """A new voice: its level is found afresh."""
        if getattr(self, "said", 0.0) > 1.0 and self.heard:
            log.info("room: that talker's level was %.3f, played at %.1f times that", math.sqrt(self.heard), self.gain or 1.0)
        self.heard: float | None = None  # the level of this talker's loud parts (mean square of full scale)
        self.gain: float | None = None
        self.said = 0.0                  # how long they've been talking, seconds

    def process(self, speech: np.ndarray) -> np.ndarray:
        level = max(MIN_LEVEL, min(MAX_LEVEL, LEVEL)) / 100.0
        target = TARGET * level
        out = np.empty(len(speech), dtype=np.float32)
        step = self.rate // 10
        for i in range(0, len(speech), step):
            x = speech[i:i + step].astype(np.float32)
            ms = float(np.mean(x * x)) / 32768.0 ** 2 if len(x) else 0.0
            if self.heard is None:
                if ms > pcm.SILENCE_FLOOR ** 2:
                    self.heard = ms
            elif ms > self.heard:
                self.heard += (ms - self.heard) * 0.6
            elif ms > self.heard / 16:                               # (quieter than a quarter of it: a gap, not their voice)
                self.heard += (ms - self.heard) * (1.0 - math.exp(-len(x) / (FALL_S * self.rate)))
            want = level * (1.0 if self.heard is None else max(MIN_GAIN, min(MAX_GAIN, TARGET / math.sqrt(self.heard))))
            if self.heard is not None:
                self.said += len(x) / self.rate
            over = OVER_TARGET if self.said > 1.0 else 1.0           # (their first second: the level isn't known yet, so nothing over it)
            most = over * target / math.sqrt(ms) if ms > 0 else want
            want = min(want, most)
            was = want if self.gain is None else min(self.gain, most)
            self.gain = want
            out[i:i + step] = x * np.linspace(was, want, len(x), endpoint=False, dtype=np.float32)   # (no step in the level: no click)
        return pcm.soft_limit(out)


def _udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    return s


class RoomStream(ReceiverStream):
    """A room, joined, as the station sees a stream (see radio.RadioStream)."""

    def __init__(self, url: str, callsign: str | None = None, sock: Callable = _udp, decoder: Callable = load_decoder,
                 clock: Callable[[], float] = time.monotonic, ids: Callable[[str], str | None] | None = None) -> None:
        super().__init__(url, clock=clock)
        self.room = parse(url)
        self.callsign = CALLSIGN if callsign is None else callsign
        self._sock_factory, self._decoder_factory = sock, decoder
        self._callsign = ids or (lambda number: dmr_ids.shared().callsign(number))    # whose a DMR ID is (dmr_ids.py)
        self.name = self.room["name"] if self.room else ""
        self.title = self.name or None
        self.talker: str | None = None
        self.connected: int | None = None
        self.heard: collections.deque = collections.deque(maxlen=HEARD)       # [callsign, when (time.time()), seconds]
        self.has_decoder: bool | None = None
        self.levelled = True             # (its level is its own: the station doesn't level it like a stream -- see Shaper)
        self._over = False               # someone has the air (on FCS their callsign comes a moment after they start)
        self._bye: tuple | None = None   # what to say on leaving, and to whom

    def rig(self, since: int = 0) -> dict:
        return {"kind": "room", "base": self.url, "name": self.name, "title": self.title, "ended": self.ended, "tunes": False,
                "talker": self.talker, "connected": self.connected, "decoder": self.has_decoder,
                "heard": [{"call": c, "at": round(at), "s": round(s, 1)} for c, at, s in list(self.heard)[::-1]],
                "freq": None, "mode": None, "dbm": None, "lo": None, "hi": None, "n": 0, "rows": []}

    def tune(self, *args, **kwargs) -> None:
        raise ValueError("a room isn't tuned: choose another room")

    def _run(self) -> None:
        sock = None
        try:
            room = self.room
            if room is None:
                raise ValueError("that isn't a room's address")
            call = clean_callsign(self.callsign)
            if not call:
                raise ValueError("rooms need your callsign: give the radio one first (Find a room)")
            decoder = self._decoder_factory()
            self.has_decoder = decoder is not None
            if decoder is None:
                log.info("room: no voice decoder on this radio: %s will show who talks, without sound", self.name)
            where = (socket.gethostbyname(room["host"]), room["port"])
            sock = self._sock_factory()
            fcs = room["net"] == "fcs"
            # "I'm here": a YSF reflector takes the callsign; an FCS one the callsign (six letters) and the room
            hello = (b"PING" + call[:6].ljust(6).encode() + room["id"].encode() + bytes(7)) if fcs else b"YSFP" + call.ljust(10).encode()
            self._bye = (b"CLOSE      " if fcs else b"YSFU" + call.ljust(10).encode(), where)
            every, resample, shaper = (FCS_POLL_S if fcs else POLL_S), Resampler(VOICE_RATE), Shaper()
            polled = asked = -1e9
            answered = self._clock()
            last_frame, over_from, introduced = 0.0, 0.0, False
            while not self._closed.is_set():
                now = self._clock()
                if now - polled >= every:
                    sock.sendto(hello, where)
                    polled = now
                if not fcs and now - asked >= STATUS_S:
                    sock.sendto(b"YSFS", where)
                    asked = now
                if self._over and now - last_frame > OVER_S:                      # the over has ended
                    self._end_over(last_frame - over_from)
                if now - answered > GONE_S:
                    raise ValueError("the room stopped answering")
                try:
                    d, _ = sock.recvfrom(400)
                except (TimeoutError, socket.timeout):
                    continue
                answered = now
                if fcs:
                    if len(d) in (7, 10) and not introduced:                      # it has us: say what we are (a listener)
                        sock.sendto(f"{0:9d}{0:9d}{'':6s}{'SleepRadio':12s}{0:7d}".ljust(100).encode(), where)
                        introduced = True
                    if len(d) != 130:
                        continue
                    payload, who = d[:120], None
                elif d[:4] == b"YSFS" and len(d) >= 42:
                    count = d[39:42].decode(errors="replace").strip()
                    self.connected = int(count) if count.isdigit() else None
                    continue
                elif d[:4] == b"YSFD" and len(d) >= 155:
                    payload, who = d[35:155], d[14:24].decode(errors="replace").strip() or None
                else:
                    continue
                head = fich(payload)
                if head is not None and head["fi"] == 2:                          # the end of the over
                    if self._over:
                        self._end_over(last_frame - over_from)
                    continue
                if not self._over:
                    self._over, over_from = True, now
                    if decoder is not None:
                        decoder.reset()
                    shaper.reset()
                last_frame = now
                dn = head is not None and head["fi"] == 1 and head["dt"] == 2
                if who is None and dn and head["fn"] == 1:                        # (FCS: who it's from rides with the voice)
                    sent = dch(payload)
                    who = sent.decode("ascii", errors="replace").strip() if sent is not None else None
                    who = who if who and re.fullmatch(r"[A-Z0-9/\- ]{3,10}", who) else None
                if who and who.isdigit():                                         # (in from DMR: under their DMR ID, not a callsign)
                    who = self._callsign(who) or who
                if who and who != self.talker:
                    if self.talker is not None:                                   # (another voice with no gap between)
                        self._end_over(last_frame - over_from)
                        self._over, over_from = True, now
                        if decoder is not None:
                            decoder.reset()
                        shaper.reset()
                    self.talker = who
                    self.title = f"{who} · {self.name}"
                frames, agree = voice(payload)
                if decoder is None or not (dn if head is not None else agree >= 0.93):   # (a header, the end, data, or the wide mode)
                    continue
                speech = [decoder.decode(bits) for bits in frames if not is_silence(bits)]
                if speech:
                    self._audio(resample.process(shaper.process(np.concatenate(speech))))
        except Exception as e:
            if not self._closed.is_set():
                self._end(str(e) if isinstance(e, ValueError) else f"the room couldn't be reached ({str(e)[:80]})")
        finally:
            if sock is not None:
                try:
                    if self._bye is not None:
                        sock.sendto(*self._bye)
                except OSError:
                    pass
                sock.close()

    def _end_over(self, seconds: float) -> None:
        self.heard.append([self.talker or "?", time.time(), max(0.0, seconds)])
        self.talker, self.title, self._over = None, self.name, False
