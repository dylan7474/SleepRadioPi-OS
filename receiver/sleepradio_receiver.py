#!/usr/bin/env python3
"""Sleep Radio receiver: an RTL-SDR dongle as a radio station on the network.

Runs on a small computer next to the aerial (a Raspberry Pi 2 is plenty) and
turns its dongle into something a Sleep Radio -- or any internet radio player
-- can tune in to as an ordinary station:

    http://RECEIVER:8074/audio?band=2m          every channel of a band at once
    http://RECEIVER:8074/audio?band=2m&hold=145.5   ...staying on one of them
    http://RECEIVER:8074/audio?freq=145.5       one frequency (narrow FM)
    http://RECEIVER:8074/audio?freq=95.0&mode=wfm   broadcast FM
    http://RECEIVER:8074/audio?band=fm              the broadcast band: the station it was last on
    http://RECEIVER:8074/audio?band=fm&hold=96.6    ...or that one
    http://RECEIVER:8074/audio?freq=28.5&mode=usb   one frequency, tuned like a rig: usb, lsb or cw (Morse)
    http://RECEIVER:8074/audio?band=10m             ...or a band of that kind, where it was last
    http://RECEIVER:8074/audio?freq=9.41&mode=am    a short-wave broadcast station (with an upconverter)

A band is watched all at once (rtl_airband takes the whole 2.4 MHz the dongle
hears apart into its channels, each with its own squelch), so it's a scanner
that never misses the start of a call: whichever channel opens is what you
hear, a priority channel takes over from the others, and the stream carries
the channel's name as its "now playing" title. A channel that stays open for
half a minute (a repeater's carrier, a stuck gateway) gives way to another
that opens; one that's always open can be skipped (/skip), which is
remembered. With nobody transmitting the
stream is silence, so a player stays tuned in.

The broadcast FM band is a band too, of another kind: it's twenty megahertz
wide and each station wants the dongle to itself, so it's one station at a
time (the one it's held on, moved with /hold, whoever is listening staying
tuned in), not a scanner.

2 metres and marine VHF are built in. Any other stretch of spectrum the
dongle can hear at once (1.9 MHz) becomes a band by saying where it starts
and ends and how far apart its channels are (POST /bands); those are kept
too.

Sideband speech and Morse are a third kind (sleepradio_tuner.py: ours, since
neither of the programs above does them): one frequency, tuned like a rig,
moved with /tune while whoever is listening stays tuned in, with a waterfall
of the quarter megahertz round it. 12 m, CB, 10 m, 6 m and the weak-signal
end of 2 m are built in as bands of this kind. AM below 30 MHz is tuned this
way too (a broadcast station, not a channel with a squelch).

An RTL-SDR starts at 24 MHz. With an upconverter between the aerial and the
dongle (--upconverter 125: a Ham It Up's oscillator, in MHz) the receiver is a
short-wave one instead: every frequency is still the real one, the dongle is
sent to that much above it, and the built-in bands are the amateur ones from
160 m to 10 m and the broadcast ones from medium wave to 16 m. --trim takes
off what the oscillator and the dongle are out by between them, in Hz.

The audio is a WAV stream (16-bit mono) with ICY titles. /status says what
it's doing as JSON; /bands lists the bands. One dongle does one thing at a
time: the latest request wins, and listeners to what it was doing before are
disconnected. With no listener for a while the dongle is left to rest.

Only the Python standard library is needed, plus rtl_airband (built with NFM)
and, for broadcast FM, rtl_fm from the rtl-sdr package; sideband and Morse
need numpy (python3-numpy) and rtl_tcp (the rtl-sdr package again).
"""

from __future__ import annotations

import argparse
import array
import base64
import collections
import json
import logging
import math
import os
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
SPECTRUM_PORT = UDP_BASE - 1 # ...and, built with rtl_airband_spectrum.py, its spectrum to this, ten times a second
SPECTRUM_ROWS = 50           # how many of those are kept for whoever draws a waterfall (5 s)
HANG_S = 2.0                 # stay with a channel this long after it closes, for the reply
HOG_S = 30.0                 # a channel open this long without a break gives way to another that opens
HIGHPASS_HZ = 300            # narrow FM: below the speech (takes out CTCSS tones, a hum otherwise)
IDLE_S = 60.0                # no listener this long: the dongle rests
MAX_CHANNELS = 64            # in one band (48 use most of one of a Pi 2's cores)
MIN_HZ, MAX_HZ = 24_000_000, 1_766_000_000      # what an RTL-SDR tunes
UP_MIN_HZ, UP_MAX_HZ = 100_000, 60_000_000      # ...and what an upconverter in front of it brings
SHORT_WAVE_HZ = 30_000_000   # AM below this is a broadcast station, tuned like a rig; above it, a channel with a squelch
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

def broadcast_channels() -> list[dict]:
    """Broadcast FM, 87.5-108 MHz every 100 kHz."""
    return [{"freq": hz, "name": f"{hz / 1e6:.1f} FM"} for hz in range(87_500_000, 108_000_001, 100_000)]


FM_START = 95_000_000        # where the broadcast band starts off, until it's been tuned
TUNE_MODES = ("usb", "lsb", "cw")       # one frequency, tuned like a rig (sleepradio_tuner.py)
RIG_MODES = (*TUNE_MODES, "am")         # ...as AM is on short wave
TUNE_RATE = 12_000           # ...and its audio


def rig(spec: dict | None) -> bool:
    """Is this tuned like a rig? Sideband and Morse always; AM in a tuned band, or anywhere on short wave."""
    if not spec:
        return False
    return spec.get("mode") in TUNE_MODES or (spec.get("mode") == "am" and (bool(spec.get("of")) or spec.get("freq", MAX_HZ) < SHORT_WAVE_HZ))


def limits_for(shift_hz: int = 0) -> tuple[int, int]:
    """What can be tuned: an RTL-SDR's own reach, or an upconverter's."""
    return (UP_MIN_HZ, UP_MAX_HZ) if shift_hz else (MIN_HZ, MAX_HZ)


def _reach(limits: tuple[int, int]) -> str:
    return f"this receiver tunes from {limits[0] / 1e6:g} to {limits[1] / 1e6:g} MHz"


def tuned_band(name: str, lo: float, hi: float, start: float, mode: str = "usb") -> dict:
    """A band that's tuned across, not a set of channels: where it runs (MHz), and where and how it starts."""
    return {"name": name, "mode": mode, "range": [_hz(lo), _hz(hi)], "start": _hz(start), "channels": []}


def _hz(value) -> int:
    """145.5 (MHz) or 145500000 (Hz) -> Hz."""
    f = float(value)
    return int(round(f * 1e6)) if f < 100_000 else int(round(f))


DEFAULT_BANDS = {
    "2m": {"name": "2 metres", "mode": "nfm", "channels": two_metre_channels(NEAR_GUISBOROUGH, skip=(145_662_500,))},
    "marine": {"name": "Marine VHF", "mode": "nfm", "channels": marine_channels()},
    "fm": {"name": "Broadcast FM", "mode": "wfm", "channels": broadcast_channels()},      # (one station at a time)
    # tuned like a rig, sideband and Morse: what an RTL-SDR reaches of short wave, and the weak-signal ends of VHF
    "12m": tuned_band("12 metres", 24.89, 24.99, 24.95),
    "cb": tuned_band("CB", 26.965, 27.405, 27.385, "lsb"),
    "10m": tuned_band("10 metres", 28.0, 29.7, 28.4),
    "6m": tuned_band("6 metres", 50.0, 50.5, 50.15),
    "2m-ssb": tuned_band("2 m sideband", 144.0, 144.4, 144.3),
}

# With an upconverter: short wave. The amateur bands and the broadcast ones, in the order they come.
HF_BANDS = {
    "mw": tuned_band("Medium wave", 0.531, 1.602, 0.909, "am"),
    "160m": tuned_band("160 metres", 1.81, 2.0, 1.9, "lsb"),
    "80m": tuned_band("80 metres", 3.5, 3.8, 3.7, "lsb"),
    "60m": tuned_band("60 metres", 5.2585, 5.4065, 5.3985),
    "49m": tuned_band("49 m broadcast", 5.9, 6.2, 6.05, "am"),
    "40m": tuned_band("40 metres", 7.0, 7.2, 7.1, "lsb"),
    "41m": tuned_band("41 m broadcast", 7.2, 7.45, 7.3, "am"),
    "31m": tuned_band("31 m broadcast", 9.4, 9.9, 9.65, "am"),
    "30m": tuned_band("30 metres", 10.1, 10.15, 10.12, "cw"),
    "25m": tuned_band("25 m broadcast", 11.6, 12.1, 11.85, "am"),
    "22m": tuned_band("22 m broadcast", 13.57, 13.87, 13.7, "am"),
    "20m": tuned_band("20 metres", 14.0, 14.35, 14.2),
    "19m": tuned_band("19 m broadcast", 15.1, 15.8, 15.4, "am"),
    "16m": tuned_band("16 m broadcast", 17.48, 17.9, 17.7, "am"),
    "17m": tuned_band("17 metres", 18.068, 18.168, 18.13),
    "15m": tuned_band("15 metres", 21.0, 21.45, 21.25),
    "12m": DEFAULT_BANDS["12m"],
    "cb": DEFAULT_BANDS["cb"],
    "10m": DEFAULT_BANDS["10m"],
}


def load_skips(path: Path | None) -> set[int]:
    try:
        return {int(f) for f in json.loads(path.read_text())} if path is not None else set()
    except (OSError, ValueError, TypeError):
        return set()


def _mhz(hz: int) -> str:
    return f"{hz / 1e6:.4f}".rstrip("0").rstrip(".")


def make_band(body: dict, limits: tuple[int, int] = (MIN_HZ, MAX_HZ)) -> dict:
    """A band from what was asked for -> {"name", "mode", "channels"}; ValueError says what's wrong.

    Either its channels one by one ({"channels": [{"freq", "name"?, "priority"?}]}) or a stretch
    of spectrum ({"from": MHz, "to": MHz, "step": kHz}: a channel every step, both ends
    included), with "name", "mode" (nfm or am) and, for a stretch, "priority": the frequency
    that takes over from the others."""
    name = " ".join(str(body.get("name") or "").split())[:40]
    if not name:
        raise ValueError("the band needs a name")
    mode = str(body.get("mode") or "nfm").lower()
    if mode not in ("nfm", "am"):
        raise ValueError("a band's mode is nfm or am")
    try:
        if body.get("channels"):
            chans = [{"freq": _hz(c["freq"]), "name": " ".join(str(c.get("name") or "").split())[:40] or _mhz(_hz(c["freq"])),
                      **({"priority": True} if c.get("priority") else {})} for c in body["channels"]]
        else:
            lo, hi, step = _hz(body["from"]), _hz(body["to"]), int(round(float(body.get("step") or 12.5) * 1000))
            if hi < lo:
                lo, hi = hi, lo
            centre_for([lo, hi])                         # (too wide for one dongle: it says so)
            if step < 5000:
                raise ValueError("channels are at least 5 kHz apart")
            if (hi - lo) // step >= MAX_CHANNELS:
                raise ValueError(f"that's {(hi - lo) // step + 1} channels: a band has {MAX_CHANNELS} at most (a wider step, or a shorter stretch)")
            first = _hz(body["priority"]) if body.get("priority") else None
            chans = [{"freq": hz, "name": _mhz(hz), **({"priority": True} if hz == first else {})} for hz in range(lo, hi + 1, step)]
    except (KeyError, TypeError, AttributeError):
        raise ValueError("a band is its channels, or from, to (MHz) and step (kHz)") from None
    except ValueError as e:
        raise ValueError(str(e) if "channel" in str(e) else "frequencies are numbers: 446.00625 (MHz)") from None
    once: dict[int, dict] = {}
    for c in chans:
        once.setdefault(c["freq"], c)                    # (each frequency once: the first to name it)
    chans = sorted(once.values(), key=lambda c: c["freq"])
    if not chans or len(chans) > MAX_CHANNELS:
        raise ValueError(f"a band has 1 to {MAX_CHANNELS} channels")
    if not all(limits[0] <= c["freq"] <= limits[1] for c in chans):
        raise ValueError(_reach(limits))
    centre_for([c["freq"] for c in chans])               # (too wide for one dongle: it says so)
    return {"name": name, "mode": mode, "channels": chans}


def band_id(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:30] or "band"


def load_bands(path: Path | None, into: dict | None = None, limits: tuple[int, int] = (MIN_HZ, MAX_HZ)) -> dict:
    """The built-in bands, with a JSON file's on top ({"id": {"name", "mode", "channels": [{"freq", "name"}]}});
    one in the file that can't be used is left out."""
    bands = {k: dict(v) for k, v in DEFAULT_BANDS.items()} if into is None else into
    if path is not None and path.is_file():
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("%s can't be read: %s", path, e)
            return bands
        for key, band in found.items():
            try:
                bands[str(key)] = make_band({"name": band.get("name") or key, "mode": band.get("mode"), "channels": band["channels"]}, limits)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                log.warning("band %s in %s left out: %s", key, path, e)
    return bands


# --- what to listen to -----------------------------------------------------------------


def parse_spec(query: dict, bands: dict, limits: tuple[int, int] = (MIN_HZ, MAX_HZ)) -> dict:
    """?band=2m[&hold=145.5] | ?freq=145.5[&mode=nfm|am|wfm|usb|lsb|cw][&squelch=off] -> a spec; ValueError says
    what's wrong. freq and hold are in MHz (145.5) or Hz (145500000). hold: the band, staying on that
    one of its channels (the whole band is still received: letting go of it is instant)."""
    one = lambda k: (query.get(k) or [None])[0] if isinstance(query.get(k), list) else query.get(k)
    band, freq = one("band"), one("freq")
    if band:
        if band not in bands:
            raise ValueError(f"no band called {band}: there is {', '.join(sorted(bands))}")
        hold = one("hold")
        if hold in (None, ""):
            return {"band": band}
        try:
            hz = _hz(hold)
        except (TypeError, ValueError):
            raise ValueError("hold is a frequency: 145.5 (MHz)") from None
        if "range" in bands[band]:
            if not bands[band]["range"][0] <= hz <= bands[band]["range"][1]:
                raise ValueError(f"{_mhz(hz)} MHz isn't in {bands[band]['name']}")
        elif not any(c["freq"] == hz for c in bands[band]["channels"]):
            raise ValueError(f"{_mhz(hz)} MHz isn't a channel of {bands[band]['name']}")
        return {"band": band, "hold": hz}
    if freq is None:
        raise ValueError("say what to listen to: ?band=NAME or ?freq=MHZ")
    try:
        f = float(freq)
    except (TypeError, ValueError):
        raise ValueError("freq is a number: 145.5 (MHz) or 145500000 (Hz)") from None
    hz = _hz(f)
    if not limits[0] <= hz <= limits[1]:
        raise ValueError(_reach(limits))
    mode = (one("mode") or "nfm").lower()
    if mode not in ("nfm", "am", "wfm", *TUNE_MODES):
        raise ValueError("mode is nfm, am, wfm, usb, lsb or cw")
    spec = {"freq": hz, "mode": mode}
    if mode in ("nfm", "am") and not rig(spec) and str(one("squelch") or "").lower() in ("off", "0", "false", "no"):
        spec["squelch"] = False
    return spec


def spec_channels(spec: dict, bands: dict, skips: set[int] = frozenset()) -> tuple[str, list[dict]]:
    """(what to call it, its channels: a band's, less the ones being skipped)."""
    if "band" in spec:
        chans = [c for c in bands[spec["band"]]["channels"] if c["freq"] not in skips]
        return bands[spec["band"]]["name"], chans or bands[spec["band"]]["channels"]
    mhz = f"{spec['freq'] / 1e6:.4f}".rstrip("0").rstrip(".")
    if rig(spec):                                     # (tuned like a rig: the frequency in full, and how)
        said = f"{spec['freq'] / 1e6:.{5 if spec['freq'] % 100 else 4}f} MHz {spec['mode'].upper()}"     # (to 10 Hz where it's tuned that finely)
        return said, [{"freq": spec["freq"], "name": said}]
    if spec.get("of"):                                # (a station of a broadcast band: by its name there)
        named = [c for c in bands[spec["of"]]["channels"] if c["freq"] == spec["freq"]]
        return (named[0]["name"] if named else f"{mhz} MHz"), [{"freq": spec["freq"], "name": f"{mhz} MHz"}]
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
                 ppm: int = 0, device: int = 0, udp_base: int = UDP_BASE, shift_hz: int = 0) -> str:
    """rtl_airband's configuration: every channel at once, each one's audio to
    its own UDP port on this machine, sent only while its squelch is open.
    shift_hz: what an upconverter adds to every frequency."""
    centre = centre_for([c["freq"] for c in channels]) + shift_hz
    rows = []
    for i, c in enumerate(channels):
        sq = "" if squelch else " squelch_threshold = -100;"
        if mode == "nfm":
            sq += f" highpass = {HIGHPASS_HZ};"
        rows.append(f'    {{ freq = {(c["freq"] + shift_hz) / 1e6:.6f}; modulation = "{mode}";{sq} outputs: ({{ type = "udp_stream"; '
                    f'dest_address = "127.0.0.1"; dest_port = {udp_base + i}; continuous = false; }}); }}')
    return (f"fft_size = {FFT_SIZE};\ndevices: ({{\n  type = \"rtlsdr\"; index = {device}; gain = {gain:.1f}; correction = {ppm};\n"
            f"  centerfreq = {centre / 1e6:.6f}; sample_rate = {SAMPLE_RATE_MS}; mode = \"multichannel\";\n  channels: (\n"
            + ",\n".join(rows) + "\n  );\n});\n")


# --- audio -----------------------------------------------------------------------------


def spectrum_row(data: bytes) -> bytes:
    """rtl_airband's spectrum (float32 mean power per FFT bin, bin 0 = the centre frequency)
    -> one byte per bin from the lowest frequency to the highest: 0 = -40 dB, 2.5 steps per dB."""
    a = array.array("f")
    a.frombytes(data[:len(data) // 4 * 4])
    if sys.byteorder == "big":
        a.byteswap()
    n, half = len(a), len(a) // 2
    out = bytearray(n)
    for k in range(n):
        p = a[(k + half) % n]
        out[k] = max(0, min(255, int((10 * math.log10(p) + 40) * 2.5))) if p > 1e-9 else 0
    return bytes(out)


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
    packet; a priority channel takes it from one that isn't; and one that has
    been open for HOG_S without a break gives it to another that opens (it's
    a carrier that's simply there, and the news is elsewhere). filler(now) is
    silence, whenever the stream has fallen behind real time -- so a player
    hears a station that's simply quiet, and never one that has stalled."""

    def __init__(self, channels: list[dict], rate: int = RATE, hang_s: float = HANG_S, hog_s: float = HOG_S) -> None:
        self.channels, self.rate, self.hang_s, self.hog_s = channels, rate, hang_s, hog_s
        self.current: int | None = None
        self.last = [0.0] * len(channels)           # when each channel last sent audio
        self.since = [0.0] * len(channels)          # ...and when its present spell began
        self.level = [0.0] * len(channels)
        self.hold: int | None = None                # listen to this channel only, whoever else speaks
        self.t0: float | None = None
        self.sent = 0                               # samples sent since t0

    def _open(self, i: int, now: float) -> bool:
        return self.last[i] > 0 and now - self.last[i] <= self.hang_s

    def active(self, now: float, within: float = 0.4) -> list[int]:
        return [i for i, t in enumerate(self.last) if t and now - t <= within]

    def packet(self, i: int, pcm: bytes, level: float, now: float) -> bytes | None:
        if not self._open(i, now):
            self.since[i] = now
        self.last[i], self.level[i] = now, level
        cur = self.current
        if self.hold is not None:
            self.current = cur = self.hold
        elif cur is None or not self._open(cur, now) or (cur != i and (
                (self.channels[i].get("priority") and not self.channels[cur].get("priority"))
                or (now - self.since[cur] > self.hog_s >= now - self.since[i]))):
            self.current = cur = i
        if cur != i:
            return None
        self._count(len(pcm) // 2, now)
        return pcm

    def title(self, now: float, idle: str) -> str:
        cur = self.current
        if self.hold is not None:
            return self.channels[self.hold]["name"]
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
                 ppm: int = 0, device: int = 0, skips_file: Path | None = None, bands_file: Path | None = None,
                 shift_hz: int = 0, trim_hz: int = 0) -> None:
        self.bands = dict(bands)
        self.shift_hz, self.trim_hz = shift_hz, trim_hz     # an upconverter's oscillator, and what it and the dongle are out by
        self.limits = limits_for(shift_hz)
        self.bands_file = bands_file                  # the bands made from the web side (POST /bands), kept here
        self.own = load_bands(bands_file, into={}, limits=self.limits)
        self.bands.update({k: b for k, b in self.own.items() if k not in bands})
        self.skips_file = skips_file
        self.skips = load_skips(skips_file)          # frequencies left out of the bands (always open, or of no interest)
        self.airband = airband or shutil.which("rtl_airband") or "/usr/local/bin/rtl_airband"
        self.rtl_fm = rtl_fm or shutil.which("rtl_fm") or "/usr/bin/rtl_fm"
        self.rtl_tcp = shutil.which("rtl_tcp") or "/usr/bin/rtl_tcp"
        self.tuner = None                             # sideband and Morse: a sleepradio_tuner.Tuner while one is being tuned
        self.rate_hz = int(SAMPLE_RATE_MS * 1e6)      # how much the dongle is hearing (the waterfall's width)
        self.zero_db, self.per_db = -40.0, 2.5        # ...and what its rows' bytes mean
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
        self.centre = 0                               # where the dongle's centre is (a band, or one frequency)
        self.spectrum: collections.deque = collections.deque(maxlen=SPECTRUM_ROWS)    # (number, spectrum_row)
        self.spectrum_n = 0
        self.at: dict[str, int] = {}                  # a broadcast band -> the station it was last on (a tuned band: where)
        self.at_mode: dict[str, str] = {}             # a tuned band -> how it was last being listened to
        self.spectrum_wanted = 0.0                    # when a waterfall last asked: nobody asking, nothing worked out
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    # (what's received)

    def select(self, spec: dict) -> None:
        """Receive this (unless it already is)."""
        with self.lock:
            if "band" in spec and self.bands[spec["band"]].get("mode") == "wfm":     # (a broadcast band: one station of it)
                return self._station(spec["band"], spec.get("hold"))
            if "band" in spec and "range" in self.bands[spec["band"]]:              # (a tuned band: where it was last)
                return self._tuned(spec["band"], spec.get("hold"))
            if spec == self.spec and self.proc is not None and self.proc.poll() is None:
                return
            if rig(spec) and self.tuner is not None and self.proc is not None and self.proc.poll() is None:
                return self._retune(spec)             # (already being tuned: moved where it is, whoever listens staying)
            self._stop()
            name, channels = spec_channels(spec, self.bands, self.skips)
            self.spec, self.name, self.error = spec, name, None
            self.gen += 1
            for lis in self.listeners:               # (they were listening to something else)
                lis.dead = True
            self.listeners = []
            self.idle_since = self.started = time.monotonic()
            self.rate_hz, self.zero_db, self.per_db = int(SAMPLE_RATE_MS * 1e6), -40.0, 2.5
            if rig(spec):
                try:
                    import sleepradio_tuner as st
                except ImportError as e:
                    self.spec = None
                    raise ValueError(f"sideband, Morse and short-wave AM need numpy on the receiver (sudo apt install python3-numpy): {e}") from None
                self.rate, self.mixer, self.title = st.AUDIO_RATE, None, name
                self.rate_hz, self.zero_db, self.per_db = st.FS, st.ZERO_DB, st.PER_DB
                self.spectrum.clear()
                gen = self.gen
                self.tuner = st.Tuner(self.rtl_tcp, audio=lambda pcm: self._send(gen, pcm), row=lambda row: self._spectrum_row(gen, row),
                                      device=self.device, gain=self.gain, ppm=self.ppm, shift_hz=self.shift_hz, trim_hz=self.trim_hz)
                try:
                    self.tuner.start(spec["freq"], spec["mode"])
                except ValueError:
                    self._stop()
                    self.spec = None
                    raise
                self.proc, self.centre = self.tuner.proc, self.tuner.centre
            elif spec.get("mode") == "wfm":
                self.rate, self.mixer, self.title = WFM_RATE, None, name
                # (said in full: this rtl_fm's "wbfm" alone gives 170 kHz audio, not 32 kHz)
                cmd = [self.rtl_fm, "-d", str(self.device), "-M", "wbfm", "-f", str(spec["freq"] + self.shift_hz + self.trim_hz), "-s", "170k",
                       "-r", str(WFM_RATE), "-E", "deemp", "-E", "dc", "-g", f"{self.gain:.1f}", "-p", str(self.ppm), "-"]
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                threading.Thread(target=self._wfm_run, args=(self.proc, self.gen), name="wfm", daemon=True).start()
            else:
                mode = spec.get("mode") or self.bands[spec["band"]].get("mode", "nfm")
                conf = airband_conf(channels, mode, spec.get("squelch", True), self.gain, self.ppm, self.device,
                                    shift_hz=self.shift_hz + self.trim_hz)
                self.centre = centre_for([c["freq"] for c in channels])
                self.spectrum.clear()
                path = Path(tempfile.gettempdir()) / "sleepradio-receiver.conf"
                path.write_text(conf)
                self.rate, self.mixer, self.title = RATE, Mixer(channels), name
                socks = []
                for i in range(len(channels)):
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", UDP_BASE + i))
                    socks.append(s)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # (the spectrum: the last of them)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", SPECTRUM_PORT))
                socks.append(s)
                self.proc = subprocess.Popen([self.airband, "-F", "-e", "-c", str(path)], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, env={**os.environ, "SPECTRUM_UDP_PORT": str(SPECTRUM_PORT)})
                threading.Thread(target=self._airband_run, args=(self.proc, socks, self.mixer, self.gen), name="airband",
                                 daemon=True).start()
            threading.Thread(target=self._stderr_run, args=(self.proc,), name="engine-log", daemon=True).start()
            log.info("receiving %s (%d channel%s)", name, len(channels), "" if len(channels) == 1 else "s")

    def _spectrum_row(self, gen: int, row: bytes) -> None:
        if gen == self.gen and time.monotonic() - self.spectrum_wanted < 10:
            self.spectrum_n += 1
            self.spectrum.append((self.spectrum_n, row))

    def _retune(self, spec: dict) -> None:
        tuner = self.tuner
        was = tuner.centre
        tuner.tune(spec["freq"], spec["mode"])
        if tuner.centre != was:                       # (the dongle moved: the waterfall begins again there)
            self.spectrum.clear()
        self.centre = tuner.centre
        self.spec, self.error = spec, None
        self.name = self.title = spec_channels(spec, self.bands)[0]
        if spec.get("of"):
            self.at[spec["of"]], self.at_mode[spec["of"]] = spec["freq"], spec["mode"]

    def tune(self, freq: int | None = None, mode: str | None = None) -> None:
        """Move what's being tuned (sideband, Morse or AM) to another frequency, or another of those modes."""
        with self.lock:
            spec = self.spec
            if self.tuner is None or not spec or self.proc is None or self.proc.poll() is not None:
                raise ValueError("nothing is being tuned: listen to a band of that kind (or ?freq=MHZ&mode=usb) first")
            mode = (mode or spec["mode"]).lower()
            if mode not in RIG_MODES:
                raise ValueError("mode is usb, lsb, cw or am")
            freq = spec["freq"] if freq is None else int(freq)
            if not self.limits[0] <= freq <= self.limits[1]:
                raise ValueError(_reach(self.limits))
            new = {"freq": freq, "mode": mode}
            of = spec.get("of")
            if of and self.bands[of]["range"][0] <= freq <= self.bands[of]["range"][1]:
                new["of"] = of
            self._retune(new)

    def _tuned(self, band: str, freq: int | None = None) -> None:
        """A tuned band: where it was last (or where it's asked for), how it was last."""
        b = self.bands[band]
        spec = {"freq": freq or self.at.get(band) or b["start"], "mode": self.at_mode.get(band) or b["mode"], "of": band}
        self.at[band], self.at_mode[band] = spec["freq"], spec["mode"]
        self.select(spec)

    def _stop(self) -> None:
        tuner, self.tuner = self.tuner, None
        if tuner is not None:
            tuner.stop()
        proc, self.proc = self.proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            time.sleep(0.3)                           # (the dongle needs a moment before it's opened again)

    def skip(self, freq: int, on: bool = True) -> None:
        """Leave a frequency out of the bands from now on (or take it back); remembered."""
        with self.lock:
            before = set(self.skips)
            (self.skips.add if on else self.skips.discard)(int(freq))
            if self.skips == before:
                return
            if self.skips_file is not None:
                try:
                    self.skips_file.parent.mkdir(parents=True, exist_ok=True)
                    self.skips_file.write_text(json.dumps(sorted(self.skips)))
                except OSError as e:
                    log.warning("couldn't keep the skipped channels: %s", e)
            log.info("%s %.4f MHz", "skipping" if on else "no longer skipping", freq / 1e6)
            spec = self.spec
            if spec and "band" in spec and any(c["freq"] == int(freq) for c in self.bands[spec["band"]]["channels"]):
                self._again()                         # the band is restarted without it

    def _again(self) -> None:
        """What's being received has changed under it: start it afresh; whoever is listening stays."""
        spec = self.spec
        if spec is None or self.proc is None:
            return
        listeners, self.listeners = self.listeners, []    # (out of select()'s way: it lets go of whoever it finds -- and
        self.spec = None                                  # one marked let-go even for a moment has its stream closed)
        self.select(spec)
        self.listeners = listeners

    def _keep_bands(self) -> None:
        if self.bands_file is not None:
            try:
                self.bands_file.parent.mkdir(parents=True, exist_ok=True)
                self.bands_file.write_text(json.dumps(self.own, indent=1))
            except OSError as e:
                log.warning("couldn't keep the bands: %s", e)

    def set_band(self, body: dict) -> str:
        """Make a band (or, with the "id" of one made here, change it); its id. ValueError says why not."""
        band = make_band(body, self.limits)
        with self.lock:
            key = str(body.get("id") or "")
            if key and key not in self.own:
                raise ValueError(f"no band of your own called {key}")
            if not key:
                key = base = band_id(band["name"])
                n = 2
                while key in self.bands:
                    key, n = f"{base}-{n}", n + 1
            self.own[key] = self.bands[key] = band
            self._keep_bands()
            log.info("band %s: %s, %d channels", key, band["name"], len(band["channels"]))
            if self.spec == {"band": key}:
                self._again()
        return key

    def remove_band(self, key: str) -> None:
        with self.lock:
            if key not in self.own:
                raise ValueError(f"{key} is built in: it can't be removed" if key in self.bands else f"no band called {key}")
            if self.spec == {"band": key}:
                for lis in self.listeners:
                    lis.dead = True
                self.listeners = []
                self.rest()
            del self.own[key], self.bands[key]
            self._keep_bands()
            log.info("band %s removed", key)

    def hold(self, freq: int | None) -> None:
        """Listen to one channel of the band being received, whoever else speaks (None: all of them again)."""
        with self.lock:
            if self.tuner is not None and self.spec:                              # being tuned: moved there
                if freq is None:
                    raise ValueError("it's tuned to one frequency: there's nothing to let go of")
                return self.tune(int(freq))
            if self.spec and self.spec.get("of") and self.proc is not None:      # a broadcast band: move to that station
                if freq is None:
                    raise ValueError(f"{self.bands[self.spec['of']]['name']} is one station at a time")
                return self._station(self.spec["of"], int(freq))
            mixer = self.mixer
            if mixer is None or self.proc is None or not self.spec or self.spec.get("mode") == "wfm":
                raise ValueError("nothing is being received to hold a channel of")
            if freq is None:
                mixer.hold = None
                return
            at = [i for i, c in enumerate(mixer.channels) if c["freq"] == int(freq)]
            if not at:
                raise ValueError(f"{_mhz(int(freq))} MHz isn't a channel of {self.name}")
            mixer.hold = at[0]

    def _station(self, band: str, freq: int | None = None) -> None:
        """One station of a broadcast band (the one it was last on, if none is asked for). Whoever is
        listening to the band stays tuned in while it moves."""
        chans = self.bands[band]["channels"]
        if freq is None:
            freq = self.at.get(band) or min((c["freq"] for c in chans), key=lambda hz: abs(hz - FM_START))
        if not any(c["freq"] == freq for c in chans):
            raise ValueError(f"{_mhz(freq)} MHz isn't a channel of {self.bands[band]['name']}")
        self.at[band] = freq
        spec = {"freq": freq, "mode": "wfm", "of": band}
        staying = self.listeners if self.spec and self.spec.get("of") == band and spec != self.spec else None
        if staying is not None:
            self.listeners = []                       # (out of select()'s way: it lets go of whoever it finds)
        self.select(spec)
        if staying is not None:
            self.listeners = staying

    def waterfall(self, since: int = 0) -> dict:
        """The spectra after number `since` (5 s of them are kept), for a waterfall."""
        with self.lock:
            self.spectrum_wanted = time.monotonic()
            running = self.proc is not None and self.proc.poll() is None and bool(self.spec) and self.spec.get("mode") != "wfm"
            rows = [(n, row) for n, row in self.spectrum if n > since] if running else []
            return {"running": running, "centre": self.centre, "rate": self.rate_hz, "n": self.spectrum_n,
                    "zero_db": self.zero_db, "per_db": self.per_db, "rows": [[n, base64.b64encode(row).decode()] for n, row in rows]}

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
                    if i == len(mixer.channels):                  # the spectrum
                        if now - self.spectrum_wanted < 10:
                            self.spectrum_n += 1
                            self.spectrum.append((self.spectrum_n, spectrum_row(data)))
                        continue
                    pcm, level = to_pcm(data)
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
            if spec is not None and "band" in spec and self.bands[spec["band"]].get("mode") == "wfm":
                self._station(spec["band"], spec.get("hold"))     # (a broadcast band: one station of it)
            elif spec is not None and "band" in spec and "range" in self.bands[spec["band"]]:
                self._tuned(spec["band"], spec.get("hold"))       # (a tuned band: where it was last)
            elif spec is not None and "band" in spec:
                hold = spec.get("hold")
                if hold in self.skips:               # (skipped: the band doesn't receive it, so it's listened to on its own)
                    self.select({"freq": hold, "mode": self.bands[spec["band"]].get("mode", "nfm")})
                else:                                # the band (as it is, if it's already being received), held or let go
                    self.select({"band": spec["band"]})
                    self.hold(hold)
            elif spec is not None:
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
            spec = self.spec                      # (a station of a broadcast band says which band)
            receiving = {"freq": spec["freq"], "mode": spec["mode"], "band": spec["of"]} if spec and spec.get("of") else spec
            out = {"receiving": receiving, "name": self.name if self.spec else None, "running": running,
                   "listeners": len(self.listeners), "title": self.title if self.spec else None, "error": self.error,
                   "rate": self.rate, "skipping": sorted(self.skips), "tunable": list(self.limits),
                   **({"upconverter": self.shift_hz} if self.shift_hz else {}),
                   "bands": {k: {"name": b["name"], "channels": len(b["channels"])} for k, b in self.bands.items()}}
            if running and self.tuner is not None:    # (tuned like a rig: /tune moves it; the passband's strength, dB of full scale)
                out["tunes"], out["level_db"] = True, round(self.tuner.level_db, 1)
            if running and mixer is not None and self.spec and self.spec.get("mode") != "wfm":
                out["hold"] = mixer.channels[mixer.hold] if mixer.hold is not None else None
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
                return self._json({k: {**b, **({"own": True} if k in receiver.own else {}),
                                       "channels": [c | ({"skip": True} if c["freq"] in receiver.skips else {}) for c in b["channels"]]}
                                   for k, b in receiver.bands.items()})
            if url.path == "/spectrum":
                try:
                    return self._json(receiver.waterfall(int((query.get("since") or ["0"])[0])))
                except ValueError:
                    return self._json({"error": "since is a number"}, 400)
            if url.path == "/":
                host = self.headers.get("Host") or f"localhost:{PORT}"
                return self._json({"what": "Sleep Radio receiver", "status": f"http://{host}/status",
                                   "listen": [f"http://{host}/audio?band={b}" for b in receiver.bands]
                                   + [f"http://{host}/audio?freq=145.5", f"http://{host}/audio?freq=95.0&mode=wfm",
                                      f"http://{host}/audio?freq=28.5&mode=usb"]})
            if url.path != "/audio":
                return self._json({"error": "not found"}, 404)
            try:
                spec = parse_spec(query, receiver.bands, receiver.limits) if query else None
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

        def do_OPTIONS(self) -> None:                 # (a web page on another machine asking first)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/select", "/skip", "/bands", "/hold", "/tune"):
                return self._json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/skip":                   # {"freq": Hz, "on": true | false}
                    receiver.skip(int(body["freq"]), body.get("on", True) is not False)
                elif path == "/tune":                 # {"freq": Hz or MHz, "mode": "usb" | "lsb" | "cw" | "am"}: either or both
                    receiver.tune(None if body.get("freq") is None else _hz(body["freq"]), body.get("mode"))
                elif path == "/hold":                 # {"freq": Hz} or {"freq": null}
                    receiver.hold(None if body.get("freq") is None else _hz(body["freq"]))
                elif path == "/bands":                # a new band, a change to one ("id"), or {"id", "remove": true}
                    if body.get("remove"):
                        receiver.remove_band(str(body.get("id")))
                    else:
                        return self._json({"id": receiver.set_band(body), **receiver.status()})
                else:
                    receiver.select(parse_spec({k: str(v) for k, v in body.items()}, receiver.bands, receiver.limits))
            except (ValueError, TypeError, AttributeError, KeyError) as e:
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
    ap.add_argument("--upconverter", type=float, default=0.0, metavar="MHZ",
                    help="an upconverter is between the aerial and the dongle, with its oscillator at this many MHz "
                         "(a Ham It Up: 125): the receiver is then a short-wave one, 0.1 to 60 MHz")
    ap.add_argument("--trim", type=int, default=0, metavar="HZ",
                    help="what the upconverter and the dongle are out by between them: how far above its real "
                         "frequency a station of known frequency is found (below: a minus)")
    ap.add_argument("--airband", help="path to rtl_airband")
    ap.add_argument("--state", type=Path, default=Path("/var/lib/sleepradio-receiver"),
                    help="where the skipped channels and the bands made from the web side are kept")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    shift = int(round(args.upconverter * 1e6))
    builtin = {k: dict(v) for k, v in (HF_BANDS if shift else DEFAULT_BANDS).items()}
    receiver = Receiver(load_bands(args.bands, into=builtin, limits=limits_for(shift)), airband=args.airband, gain=args.gain,
                        ppm=args.ppm, device=args.device, skips_file=args.state / "skip.json", bands_file=args.state / "bands.json",
                        shift_hz=shift, trim_hz=args.trim)
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
