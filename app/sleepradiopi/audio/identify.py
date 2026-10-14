"""What's this? A signal named: from where it is, how it sounds, and what the readers make of it.

The **What's this?** key in a receiver's window. The radio listens to what
it is playing for LISTEN_S and says what it most likely is, in a sentence,
and which key reads it if one does. Three things are put together:

  * Where it is (places()): most of short wave is spoken for. The amateur
    bands have a part for Morse, a part for data and a part for speech,
    and FT8, FT4, WSPR and PSK31 each keep to one frequency a band; the
    broadcasters have their bands, the aircraft and the ships theirs; the
    time signals, the weather charts and the weather teleprinters are
    always in the same place. A list of those, no more: it says what is
    *usually* there, which is half the answer and sometimes all of it
    (WSPR can't be told by ear in twelve seconds, but nothing else lives
    in its 200 Hz).
  * How it sounds (measure()): a picture of the twelve seconds, a column
    every 35 ms: what stands out of the noise and how wide each thing is,
    whether it is there all the time or comes and goes, and whether the
    whole of what's heard swells and falls as speech does.
  * What the readers make of it: the readers the radio already has are
    the judges, as they are for the survey's finder (playback/survey.py).
    It is Morse if the Morse reader reads it and what it read holds
    together; a teleprinter if a pair of tones frames; PSK31 if the eye is
    open and it turns as text does; a picture if the header or the line
    pulses are found. A reader's yes is said plainly ("sure": "read"); the
    rest is said as likely, or as a guess, and how sure is always given.

Rules, worked out against the air (a KiwiSDR and a dongle, 40 and 80 m of
an evening), not a trained network: it has to run on a Zero in a second or
two, and to be able to say *why*.

`listener` is the one the radio uses: the web side starts it (start()),
the station feeds it what a receiver plays (broadcast/station.py), and the
naming is done in a process of its own at low priority, as the readers'
is, so it never takes the processor from the thread playing the sound.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
import time

import numpy as np

from sleepradiopi.audio import morse, pcm, psk, rtty, sstv

log = logging.getLogger(__name__)

DECIMATE = 6                 # of the speaker's rate: 7350 samples a second, as the Morse reader has it
RATE = pcm.SAMPLE_RATE / DECIMATE
LISTEN_S = 12.0              # how long it listens (the Morse reader wants eight seconds to be sure)
STARVED_S = 8.0              # no sound for this long while listening: nothing is being played from a receiver
THINK_S = 60.0               # the naming given up after this long
MODES = ("usb", "lsb", "cw", "am", "fm", "nfm", "wfm", "sam", "iq")

# --- where it is ---------------------------------------------------------------------------------

_K = 1000
# what's usually where: PLACES is [(from, to: Hz, kind, what it's called)]
_FT8 = (1840, 3573, 5357, 7074, 10136, 14074, 18100, 21074, 24915, 28074, 50313, 144174)       # dial frequencies, kHz (upper sideband):
_FT4 = (3575, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180, 50318, 144170)                 # ...as the page's keys have them
_WSPR = (1836.6, 3568.6, 5287.2, 7038.6, 10138.7, 14095.6, 18104.6, 21094.6, 24924.6, 28124.6, 50293, 144489)
_PSK = (1838, 3580, 7040, 10142, 14070, 18096, 21070, 24920, 28120)
_FAX = ((3855, "Hamburg"), (7880, "Hamburg"), (13882.5, "Hamburg"), (2618.5, "Northwood"), (4610, "Northwood"), (8040, "Northwood"), (11086.5, "Northwood"))
_WEATHER = (147.3, 4583, 7646, 10100.8, 11039, 14467.3)              # the German weather service's teleprinters (the middle of the two tones)
_TIME = ((2500, "WWV"), (5000, "WWV or BPM"), (10000, "WWV or BPM"), (15000, "WWV or BPM"), (20000, "WWV"), (3330, "CHU, Canada"), (7850, "CHU, Canada"),
         (14670, "CHU, Canada"), (4996, "RWM, Moscow"), (9996, "RWM, Moscow"), (14996, "RWM, Moscow"))
_BEACONS = (14100, 18110, 21150, 24930, 28200)                       # the worldwide beacon chain: Morse, a station every ten seconds
_VOLMET = ((3413, "Shannon"), (5505, "Shannon"), (8957, "Shannon"), (13264, "Shannon"), (5450, "the RAF"), (11253, "the RAF"))
# an amateur band's parts: (band, Morse from, data from, speech from, the end), kHz
_HAM = (("160 m", 1810, 1838, 1843, 2000), ("80 m", 3500, 3570, 3600, 3800), ("40 m", 7000, 7040, 7060, 7200), ("30 m", 10100, 10130, 10150, 10150),
        ("20 m", 14000, 14070, 14101, 14350), ("17 m", 18068, 18095, 18111, 18168), ("15 m", 21000, 21070, 21151, 21450),
        ("12 m", 24890, 24915, 24931, 24990), ("10 m", 28000, 28070, 28225, 29700), ("6 m", 50000, 50100, 50100, 50500))
_BROADCAST = ((148.5, 283.5, "long wave"), (526.5, 1606.5, "medium wave"), (2300, 2495, "120 m"), (3200, 3400, "90 m"), (3900, 4000, "75 m"),
              (4750, 5060, "60 m"), (5900, 6200, "49 m"), (7200, 7450, "41 m"), (9400, 9900, "31 m"), (11600, 12100, "25 m"), (13570, 13870, "22 m"),
              (15100, 15800, "19 m"), (17480, 17900, "16 m"), (18900, 19020, "15 m"), (21450, 21850, "13 m"), (25670, 26100, "11 m"))
_AIR = ((2850, 3025), (3400, 3500), (4650, 4700), (5450, 5680), (6525, 6685), (8815, 8965), (10005, 10100), (11275, 11400), (13260, 13360), (17900, 17970))
_SEA = ((4063, 4438), (6200, 6525), (8195, 8815), (12230, 13200), (16360, 17410), (22000, 22855))


def _table() -> list[tuple[float, float, str, str]]:
    t: list[tuple[float, float, str, str]] = []
    add = lambda lo, hi, kind, name: t.append((float(round(lo * _K)), float(round(hi * _K)), kind, name))
    band = lambda f: next((b for b, lo, *_, hi in _HAM if lo <= f <= hi), "60 m" if 5250 < f < 5450 else "2 m" if f > 144000 else "the band")
    for f in _FT8:
        add(f + 0.1, f + 3.0, "ft8", f"FT8's frequency on {band(f)}")
    for f in _FT4:
        add(f + 0.1, f + 3.0, "ft4", f"FT4's frequency on {band(f)}")
    for f in _WSPR:
        add(f + 1.4, f + 1.6, "wspr", f"the 200 Hz kept for WSPR beacons on {band(f)}")
    for f in _PSK:
        add(f + 0.5, f + 2.5, "psk31", f"where PSK31 is used on {band(f)}")
    for f, who in _FAX:
        add(f - 0.6, f + 0.6, "fax", f"a weather chart service's frequency ({who})")
    for f in _WEATHER:
        add(f - 0.4, f + 0.4, "weather", "the German weather service's teleprinter")
    for f, who in _TIME:
        add(f - 0.3, f + 0.3, "time", f"a time signal's frequency ({who})")
    for f in _BEACONS:
        add(f - 0.3, f + 0.3, "beacon", "the worldwide chain of amateur beacons")
    for f, who in _VOLMET:
        add(f, f + 3.0, "volmet", f"aviation weather read out by {who}")
    for band, cw, data, phone, end in _HAM:
        add(cw, data, "ham-cw", f"the Morse part of the {band} amateur band")
        if phone > data:
            add(data, phone, "ham-data", f"the data part of the {band} amateur band")
        if end > phone:
            add(phone, end, "ham-phone", f"the speech part of the {band} amateur band")
    add(5351.5, 5366.5, "ham", "the 60 m amateur band")
    for lo, hi, name in _BROADCAST:
        add(lo, hi, "broadcast", f"the {name} broadcast band")
    for lo, hi in _AIR:
        add(lo, hi, "air", "a band for aircraft over the oceans")
    for lo, hi in _SEA:
        add(lo, hi, "sea", "a band for ships and coast stations")
    add(2182 - 1.5, 2182 + 1.5, "sea", "the ships' old calling and distress frequency")
    add(26965, 27405, "cb", "the CB band")
    add(27601, 27991, "cb", "the UK's own CB channels")
    # (a receiver of your own on VHF)
    add(87500, 108000, "broadcast", "the FM broadcast band")
    add(118000, 137000, "air-vhf", "the VHF airband")
    add(144000, 144400, "ham-cw", "the Morse and sideband end of the 2 m amateur band")
    add(144794, 144806, "aprs", "the 2 m packet frequency (APRS)")
    add(144400, 146000, "ham-fm", "the 2 m amateur band")
    add(156000, 162050, "sea-vhf", "the marine VHF band")
    add(161962, 162038, "ais", "the ships' position reports (AIS)")
    add(430000, 440000, "ham-fm", "the 70 cm amateur band")
    add(446000, 446200, "pmr", "the licence-free walkie-talkie channels (PMR 446)")
    return sorted(t, key=lambda e: e[1] - e[0])


PLACES = _table()


def heard(hz: float, mode: str) -> tuple[float, float]:
    """What's heard, on the air, with the receiver showing hz in that mode: (from, to), Hz."""
    if mode == "usb":
        return hz + 200.0, hz + 2900.0
    if mode == "lsb":
        return hz - 2900.0, hz - 200.0
    if mode == "cw":                                 # (a KiwiSDR's CW: the narrow filter, a few hundred Hz above what it shows)
        return hz + 200.0, hz + 1000.0
    return hz - 3000.0, hz + 3000.0


def places(hz: float, mode: str) -> list[dict]:
    """What's usually where the receiver is listening -> [{"kind", "name"}]: the place most of which is being
    heard first (the one frequency before the part of the band before the band), one of a kind."""
    lo, hi = heard(hz, mode)
    found: dict[str, tuple[float, float, str]] = {}
    for a, b, kind, name in PLACES:
        share = (min(b, hi) - max(a, lo)) / (b - a)
        if share > 0 and (kind not in found or share > found[kind][0]):
            found[kind] = (share, b - a, name)
    return [{"kind": kind, "name": name} for kind, (_, _, name) in sorted(found.items(), key=lambda kv: (-kv[1][0], kv[1][1]))]


def air(hz: float, mode: str, pitch: float) -> float:
    """Where on the air something at this pitch in the sound is."""
    return hz - pitch if mode == "lsb" else hz + pitch if mode in ("usb", "cw") else hz


def _at(hz: float, kinds: tuple[str, ...]) -> dict | None:
    """The place of one of these kinds that this frequency on the air is in."""
    for a, b, kind, name in PLACES:
        if kind in kinds and a <= hz <= b:
            return {"kind": kind, "name": name}
    return None


# --- how it sounds -------------------------------------------------------------------------------

BAND = (300.0, 2700.0)       # the part of the sound looked at, Hz (inside any sideband filter)
FFT = 1024                   # a column of the picture: 139 ms of sound, 7.2 Hz a bin...
HOP = 256                    # ...every 35 ms
OVER_DB = 6.0                # a thing's edges: this far over the noise
PEAK_DB = 9.0                # ...and it has to stand this far over it somewhere
WIDTH_DB = 10.0              # how wide it is: the part within this of its strongest (a strong Morse signal's keying spreads 100 Hz over the noise)
NARROW_HZ = 60.0             # a tone, keyed or not, is no wider than this
CARRIER_HZ = 32.0            # ...and a carrier, which is one pitch and no more, narrower still (FT8's eight tones: 57)
KEYED_DB = 10.0              # ...and keyed if it comes and goes by this much
STEADY_DB = 6.0              # ...or steady if by no more than this
WIDE_HZ = 300.0              # a block of data is at least this wide
SPEECH_SWING_DB = 4.0        # speech: the whole of what's heard swells and falls by this much...
SPEECH_WANDER_HZ = 170.0     # ...and its weight moves about the band by this much (noise: 50 to 150; speech: 190 and up)
SPEECH_DEPTH = 0.18          # ...the strength varying by this share of itself (noise: under 0.15)
SPEECH_FILLED = 0.12         # ...with this share of the band in use, or something broad in it (static crashes swell and wander too, and fill nothing)
FT_WIDE_HZ = (30.0, 230.0)   # an FT8 or FT4 signal as it shows here (50 or 83 Hz of tones, two side by side run together)
QUIET_SWING_DB = 3.0         # nothing but noise: steadier than this, with nothing standing out


def measure(x: np.ndarray, rate: float = RATE) -> dict:
    """The sound as a picture, and what's in it -> {"things": [{"hz" (its peak), "lo", "hi", "wide" (Hz), "db"
    (over the noise), "swing" (how much it comes and goes, dB), "on" (the share of the time it's there)}] the
    strongest first, "swing" (of everything heard, dB), "depth", "wander" (Hz), "filled" (the share of the band
    with something in it), "fax" (the share of the sound between a chart's black and white)}."""
    x = np.asarray(x, np.float32)
    x = x / (float(np.sqrt(np.mean(x ** 2))) + 1e-9)
    k = (len(x) - FFT) // HOP + 1
    if k < 8:
        return {"things": [], "swing": 0.0, "depth": 0.0, "wander": 0.0, "filled": 0.0, "fax": 0.0}
    frames = np.lib.stride_tricks.sliding_window_view(x, FFT)[::HOP][:k] * np.hanning(FFT).astype(np.float32)
    p = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    f = np.fft.rfftfreq(FFT, 1 / rate)
    band = (f >= BAND[0]) & (f <= BAND[1])
    mean = p.mean(axis=0)
    db = 10 * np.log10(mean + 1e-12)
    floor = float(np.percentile(db[band], 20))
    noise = 10 ** (floor / 10)                       # (a bin of noise)
    up = (db > floor + OVER_DB) & band
    things, i = [], 0
    while i < len(up):
        if not up[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(up) and up[j + 1]:
            j += 1
        peak = float(db[i:j + 1].max() - floor)
        if peak >= PEAK_DB:
            e = p[:, i:j + 1].sum(axis=1)
            e_db = 10 * np.log10(e + 1e-12)
            near = np.flatnonzero(db[i:j + 1] >= db[i:j + 1].max() - WIDTH_DB)       # (its width: what's within WIDTH_DB of its best)
            things.append({"hz": float(f[i + int(np.argmax(db[i:j + 1]))]), "lo": float(f[i]), "hi": float(f[j]),
                           "wide": float((near[-1] - near[0] + 1) * rate / FFT), "db": round(peak, 1),
                           "swing": round(float(np.percentile(e_db, 90) - np.percentile(e_db, 10)), 1),
                           "on": round(float(np.mean(e > noise * (j - i + 1) * 4)), 2)})
        i = j + 1
    things.sort(key=lambda t: -t["db"])
    whole = p[:, band].sum(axis=1)
    whole_db = 10 * np.log10(whole + 1e-12)
    amp = np.sqrt(whole)
    centre = (p[:, band] * f[band]).sum(axis=1) / (whole + 1e-12)
    chart = (f >= 1400.0) & (f <= 2400.0)
    return {"things": things, "swing": round(float(np.percentile(whole_db, 90) - np.percentile(whole_db, 10)), 1),
            "depth": round(float(np.std(amp) / (np.mean(amp) + 1e-12)), 2), "wander": round(float(np.std(centre))),
            "filled": round(float(np.mean(up[band])), 2),
            "fax": round(float((mean[chart] - noise).clip(0).sum() / ((mean[band] - noise).clip(0).sum() + 1e-12)), 2)}


# --- what the readers make of it -----------------------------------------------------------------

MORSE_QUALITY = 0.6          # what the Morse reader read holds together this well, or it isn't said to be Morse...
MORSE_WEAK = 0.3             # ...or, in the Morse part of an amateur band, this well (off the air: contest exchanges at 0.38)
MORSE_WPM = (10.0, 45.0)     # ...at a speed people send at (an FT8 signal reads as "Morse" at 8 or 9)
MORSE_LETTERS = 4
MORSE_TRIED = 2              # keyed tones the Morse reader is given, the strongest
RTTY_TRIED = 2
PSK_TRIED = 3
PSK_LETTERS = 6              # a PSK31 signal has to say this many letters...
PSK_SURE = 0.8               # ...each as sure as this, on average (PSK31: 0.99; Morse read as PSK31 says "letters" too, at 0.54)


def _morse(x: np.ndarray, rate: float, pitch: float) -> dict | None:
    """What the Morse reader makes of the tone at this pitch -> {"hz", "wpm", "quality", "text", "named" (it
    read a callsign or a word of the trade)}; None if nothing that stands up."""
    read = morse.decode(x, rate, pitch, morse.noise_level(x, rate, pitch)[2])
    text = " ".join(read["text"].split())
    letters = sum(c.isalnum() for c in text)
    if letters < MORSE_LETTERS or not MORSE_WPM[0] <= read["wpm"] <= MORSE_WPM[1]:
        return None
    words = [w for w in text.split() if len(w) > 1]
    named = any(w in morse.VOCAB or morse.CALLSIGN.fullmatch(w) for w in words)
    return {"hz": float(read["pitch"]), "wpm": round(read["wpm"]), "quality": float(read["quality"]), "text": text[:60], "named": named}


def _rtty(x: np.ndarray, rate: float) -> dict | None:
    """The teleprinter in the sound, if a pair of tones frames -> {"hz" (the middle), "shift", "baud", "text"}."""
    for low, shift, _ in rtty.find_all(x, rate, most=RTTY_TRIED):
        r = rtty.read(x, rate, low, shift)
        if len(r["codes"]) >= rtty.CHARS_MIN:
            said = "".join(c for c, *_ in rtty.text(r["codes"])[0])
            return {"hz": low + shift / 2, "shift": round(shift), "baud": r["baud"], "text": " ".join(said.split())[:60]}
    return None


def _psk(x: np.ndarray, rate: float) -> dict | None:
    """PSK31 or PSK63 in the sound, if a signal says enough letters -> {"hz", "baud" (31 or 63), "text"}."""
    for hz, _ in psk.find(x, rate, most=PSK_TRIED):
        r = max((psk.read(x, rate, hz, baud) for baud in psk.BAUDS), key=lambda r: len(r["chars"]))
        said = "".join(c for c, *_ in r["chars"])
        if (len(r["chars"]) >= PSK_LETTERS and sum(c in psk.PRINTS for c in said) >= 0.9 * len(said) and r["chars"][-1][1] - r["chars"][0][1] >= 1.0
                and float(np.mean([sure for *_, sure in r["chars"]])) >= PSK_SURE):
            return {"hz": float(r["pitch"]), "baud": psk.NAMES[r["baud"]], "text": " ".join(said.split())[:60]}
    return None


def _picture(x: np.ndarray, rate: float) -> str | None:
    """The SSTV mode being sent, by its header or its line pulses; None."""
    pitch, _ = sstv.pitch(x, rate)
    found = sstv.find_vis(pitch, rate) or sstv.find_late(pitch, rate)
    return sstv.MODES[found[0]]["name"] if found and found[0] in sstv.MODES else None


# --- and so, what it is --------------------------------------------------------------------------

KEYS = {"cw": "CW", "rtty": "RTTY", "psk31": "PSK", "ft8": "FT8", "ft4": "FT4", "wspr": "WSPR", "sstv": "SSTV", "fax": "Fax"}
NAMES = {"cw": "Morse", "rtty": "a radio teleprinter (RTTY)", "psk31": "PSK31", "ft8": "FT8", "ft4": "FT4", "wspr": "WSPR", "sstv": "slow-scan television (SSTV)",
         "fax": "a weather fax", "speech": "speech", "broadcast": "a broadcast station", "time": "a time signal", "carrier": "a steady carrier",
         "data": "a data signal", "keyed": "a keyed tone", "noise": "only noise", "unknown": "something this radio can't name"}
SIDEBAND = ("usb", "lsb", "cw")
_TALK = {"volmet": "aviation weather being read out", "ham-phone": "radio amateurs talking", "ham": "radio amateurs talking", "ham-fm": "radio amateurs talking",
         "air": "aircraft and the stations that work them", "air-vhf": "aircraft and air traffic control", "sea": "ships or a coast station",
         "sea-vhf": "ships, harbours or the coastguard", "cb": "CB operators", "pmr": "walkie-talkies"}


def _whose(where: list[dict]) -> str:
    """Who is likely talking, by the place: ": radio amateurs talking (the speech part of the 80 m amateur band)"."""
    p = next((p for p in where if p["kind"] in _TALK), None)
    return "" if p is None else f": {p['name']}" if p["kind"] == "volmet" else f": {_TALK[p['kind']]} ({p['name']})"


def identify(x: np.ndarray, hz: float | None = None, mode: str = "usb", rate: float = RATE) -> dict:
    """What a stretch of sound is, with the receiver showing hz (None: not known) in that mode -> {"kind" (a
    key of NAMES), "sure" ("read": a reader read it, "likely", "guess" or "nothing"), "says" (in a sentence or
    two), "key"? (the reader's key that reads it: "cw", "rtty", "psk31", "ft8", "ft4", "wspr", "sstv", "fax"),
    "text"? (what was read), "at"? (where on the air, Hz), "places": [what's usually there], "heard": the
    measurements}."""
    mode = mode if mode in MODES else "usb"
    m = measure(x, rate)
    where = places(hz, mode) if hz else []
    kinds = {p["kind"] for p in where}
    usual = where[0]["name"] if where else None
    things = m["things"]
    on_air = lambda pitch: air(hz, mode, pitch) if hz else None
    out: dict = {"places": where, "heard": {**{k: v for k, v in m.items() if k != "things"},
                                            "things": [{"hz": round(t["hz"]), "wide": round(t["wide"]), "db": t["db"], "swing": t["swing"]} for t in things[:6]]}}

    def say(kind: str, sure: str, says: str, key: str | None = None, text: str | None = None, pitch: float | None = None) -> dict:
        if key:
            says += f" The {KEYS[key]} key reads it."
        out.update(kind=kind, sure=sure, says=says)
        if key:
            out["key"] = key
        if text:
            out["text"] = text
        if pitch is not None and on_air(pitch):
            out["at"] = round(on_air(pitch))
        return out

    sideband = mode in SIDEBAND
    if sideband:
        # a picture, a teleprinter: the readers know their own
        picture = _picture(x, rate)
        if picture:
            return say("sstv", "read", f"Slow-scan television (SSTV): a picture being sent, in {picture}.", "sstv")
        tele = _rtty(x, rate)
        if tele:
            whose = "the German weather service's teleprinter" if _at(on_air(tele["hz"]) or 0, ("weather",)) else "a radio teleprinter (RTTY)"
            return say("rtty", "read", f"{whose[0].upper()}{whose[1:]}: {tele['baud']:g} baud, the two tones {tele['shift']} Hz apart."
                       + (f" It reads: “{tele['text']}”." if tele["text"] else ""), "rtty", tele["text"], tele["hz"])
        # FT8, FT4 and WSPR keep to their own frequencies, and nothing else lives there
        # (on 80 m FT8's three kilohertz and FT4's overlap: it's the one more of what's heard is in)
        ft = {kind: [t for t in things if (t["wide"] <= 70.0 if kind == "wspr" else FT_WIDE_HZ[0] <= t["wide"] <= FT_WIDE_HZ[1]) and _at(on_air(t["hz"]), (kind,))]
              for kind in ("wspr", "ft8", "ft4") if kind in kinds}
        for kind, there in sorted(ft.items(), key=lambda kt: -len(kt[1]) - (0.5 if kt[0] == "wspr" else 0)):
            if there:
                n = len(there)
                how = {"wspr": "beacons of a watt or so, two minutes each: too slow to tell by ear, but nothing else is sent in these 200 Hz",
                       "ft4": "bursts of four tones, four and a half seconds long", "ft8": "eight tones in turn, thirteen seconds in every fifteen"}[kind]
                return say(kind, "likely", f"{NAMES[kind]}, most likely: {n} signal{'s' if n != 1 else ''} of its width on {usual if where[0]['kind'] == kind else NAMES[kind] + chr(39) + 's frequency'} "
                           f"({how}).", kind, pitch=there[0]["hz"])
        # Morse: a keyed tone that the Morse reader reads
        best = None
        for t in [t for t in things if t["wide"] <= NARROW_HZ * 1.5 and t["swing"] >= KEYED_DB][:MORSE_TRIED]:
            r = _morse(x, rate, t["hz"])
            if r and (best is None or r["quality"] > best["quality"]):
                best = r
        if best:
            in_cw = bool(_at(on_air(best["hz"]) or 0, ("ham-cw", "beacon")))
            if best["quality"] >= MORSE_QUALITY or (in_cw and best["quality"] >= MORSE_WEAK):
                sure = "read" if best["named"] and best["quality"] >= MORSE_QUALITY else "likely"
                beacon = _at(on_air(best["hz"]) or 0, ("beacon",))
                return say("cw", sure, f"Morse{'' if sure == 'read' else ', most likely'}, about {best['wpm']} words a minute"
                           + (f" ({beacon['name']})" if beacon else "") + f". It reads: “{best['text']}”.", "cw", best["text"], best["hz"])
        typed = _psk(x, rate)
        if typed:
            return say("psk31", "read", f"PSK{typed['baud']}: text typed at a keyboard. It reads: “{typed['text']}”.", "psk31", typed["text"], typed["hz"])
        # a weather chart: everything between black (1500 Hz) and white (2300), or the one tone between charts
        tone = things[0] if things else None
        if "fax" in kinds and tone and (m["fax"] >= 0.8 or (tone["wide"] <= NARROW_HZ and 1400 <= tone["hz"] <= 2400)):
            idle = tone["wide"] <= NARROW_HZ and len(things) == 1
            return say("fax", "likely", f"A weather fax, most likely ({usual}): " + ("the steady tone between charts just now." if idle else "a chart being sent."), "fax", pitch=tone["hz"])
    quiet = not things and m["swing"] < QUIET_SWING_DB
    if "time" in kinds and things and things[0]["wide"] <= NARROW_HZ:
        return say("time", "likely", f"A time signal, most likely ({usual}): a carrier with a tick each second" + (", heard here as a tone" if sideband else "") + ".", pitch=things[0]["hz"])
    # speech: the whole of what's heard swells and falls, and its weight moves about
    broad = [t for t in things if t["wide"] >= 90.0 and t["swing"] >= 8.0]
    speech = (m["swing"] >= SPEECH_SWING_DB and m["wander"] >= SPEECH_WANDER_HZ and m["depth"] >= SPEECH_DEPTH
              and (bool(broad) or m["filled"] >= SPEECH_FILLED))
    if not sideband and "broadcast" in kinds and m["wander"] >= SPEECH_WANDER_HZ and m["depth"] >= SPEECH_DEPTH:
        speech = True                                # (a strong station's carrier holds the level steady: it's told by the other two, where stations are)
    if speech and not sideband:
        if "broadcast" in kinds:
            return say("broadcast", "likely", f"A broadcast station, most likely: speech or music in {usual}.")
        return say("speech", "likely", f"Speech, most likely{_whose(where)}.")
    if speech:
        if "broadcast" in kinds:
            return say("broadcast", "likely", f"A broadcast station, most likely, heard in sideband ({usual}): the AM key plays it properly.")
        return say("speech", "likely", f"Speech, most likely{_whose(where)}. If it sounds like a duck, it's the other sideband.")
    if not sideband:                                 # (what an AM or FM detector gives of anything else has no shape to go by)
        if "broadcast" in kinds:
            return say("noise", "nothing", f"Nothing clear in these twelve seconds. This is {usual}: a station here is too weak to make out just now, or off the air.")
        talk = next((_TALK[p["kind"]] for p in where if p["kind"] in _TALK), None)
        return say("noise", "nothing", "Nothing to go by in these twelve seconds" + (f". This is {usual}: {talk}, when somebody is there." if talk else f". This is {usual}." if usual else "."))
    if quiet:
        return say("noise", "nothing", "Only noise in these twelve seconds" + (f". This is {usual}: nobody was sending." if usual else "."))
    if not things:
        return say("unknown", "guess", "Something that comes and goes, with no tone to it: static crashes, or a signal too weak to name"
                   + (f". This is {usual}." if usual else "."))
    top = things[0]
    if top["wide"] <= CARRIER_HZ and top["swing"] <= STEADY_DB:
        if "broadcast" in kinds and sideband:
            return say("carrier", "likely", f"A broadcast station's carrier, most likely ({usual}): a steady whistle in sideband. The AM key plays the station.", pitch=top["hz"])
        return say("carrier", "likely", "A steady carrier: a whistle that never changes. A station's carrier with nothing on it, or an unwanted tone of the receiver's own"
                   + (f". This is {usual}." if usual else "."), pitch=top["hz"])
    if top["wide"] >= WIDE_HZ and top["swing"] <= STEADY_DB:
        return say("data", "guess", f"A data signal about {round(top['wide'] / 100) * 100:g} Hz wide, sent without a pause: not one this radio reads"
                   + (f". This is {usual}." if usual else "."), pitch=top["hz"])
    if FT_WIDE_HZ[0] <= top["wide"] <= FT_WIDE_HZ[1] and sideband:
        return say("data", "guess", f"A narrow data signal, about {round(top['wide'] / 10) * 10:g} Hz wide" + (f". This is {usual}." if usual else ": the width of FT8 or FT4, but not on a frequency of theirs."), pitch=top["hz"])
    if top["wide"] <= NARROW_HZ * 1.5:
        return say("keyed", "guess", "A tone that comes and goes, but nothing the Morse reader could read: Morse too weak or too fast, or bursts of data"
                   + (f". This is {usual}." if usual else "."), pitch=top["hz"])
    return say("unknown", "guess", f"Something about {round(top['wide'] / 100) * 100:g} Hz wide that this radio can't name" + (f". This is {usual}." if usual else "."), pitch=top["hz"])


# --- the one the radio uses ----------------------------------------------------------------------

def _think(conn, x: np.ndarray, hz: float | None, mode: str) -> None:
    """The naming, in a process of its own: the answer (or {"error"}) sent back."""
    try:
        os.nice(10)                                  # (the sound comes first)
    except OSError:
        pass
    try:
        conn.send(identify(x, hz, mode))
    except Exception as e:                           # (said, not left hanging)
        log.exception("identify: the naming failed")
        conn.send({"error": f"it couldn't be worked out ({type(e).__name__})"})


class Listener:
    """What's this?, as the radio does it: start() it, feed() it what a receiver plays, ask status() until
    it's "done" or "failed". One at a time: starting again drops the one under way."""

    def __init__(self, own_process: bool = True, clock=time.monotonic) -> None:
        self._own_process, self._clock = own_process, clock
        self._lock = threading.Lock()
        self._n = 0                                  # which asking this is
        self._state = "idle"
        self._hz: float | None = None
        self._mode = "usb"
        self._got: list[np.ndarray] = []
        self._have = 0
        self._left = np.zeros(0, np.float32)         # (samples waiting to make up a whole DECIMATE)
        self._last = 0.0                             # when sound last came
        self._result: dict | None = None
        self._error: str | None = None

    def start(self, hz=None, mode="usb") -> dict:
        """Listen from now, the receiver showing hz (Hz; None if it isn't known) in mode. ValueError says what's wrong."""
        if hz is not None and (isinstance(hz, bool) or not isinstance(hz, (int, float)) or not 0 < hz < 3e9):
            raise ValueError("hz is where the receiver is tuned, in Hz")
        mode = str(mode or "usb").lower()[:8]
        mode = mode if mode in MODES else "nfm" if mode == "nbfm" else "am"       # (whatever else a receiver calls a mode: not a sideband)
        with self._lock:
            self._n += 1
            self._state, self._hz, self._mode = "listening", (float(hz) if hz else None), mode
            self._got, self._have, self._left = [], 0, np.zeros(0, np.float32)
            self._last, self._result, self._error = self._clock(), None, None
            n = self._n
        threading.Thread(target=self._run, args=(n,), name="identify", daemon=True).start()
        return self.status()

    def feed(self, block: np.ndarray) -> None:
        """What's being played (a block at the speaker's rate). Cheap; nothing at all unless it's listening."""
        if self._state != "listening":
            return
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        with self._lock:
            if self._state != "listening":
                return
            mono = np.concatenate((self._left, mono)) if len(self._left) else mono
            whole = len(mono) // DECIMATE * DECIMATE
            self._left = mono[whole:]
            if whole:
                self._got.append(mono[:whole].reshape(-1, DECIMATE).mean(axis=1))
                self._have += whole // DECIMATE
            self._last = self._clock()

    def status(self) -> dict:
        """{"state": "idle" | "listening" | "thinking" | "done" | "failed", "n" (which asking), "hz", "mode",
        "left_s" (of the listening), "result"? (identify()'s, when done), "error"? (when failed)}"""
        with self._lock:
            out = {"state": self._state, "n": self._n, "hz": self._hz, "mode": self._mode,
                   "left_s": round(max(0.0, LISTEN_S - self._have / RATE), 1) if self._state == "listening" else 0.0}
            if self._result is not None:
                out["result"] = self._result
            if self._error is not None:
                out["error"] = self._error
            return out

    def _end(self, n: int, result: dict | None, error: str | None = None) -> None:
        with self._lock:
            if n == self._n:
                self._state, self._result, self._error = ("done" if result is not None else "failed"), result, error
                self._got = []

    def _run(self, n: int) -> None:
        while True:                                  # the listening
            time.sleep(0.1)
            with self._lock:
                if n != self._n:
                    return
                if self._have >= LISTEN_S * RATE:
                    x = np.concatenate(self._got)[:int(LISTEN_S * RATE)]
                    self._state, self._got = "thinking", []
                    hz, mode = self._hz, self._mode
                    break
                starved = self._clock() - self._last > STARVED_S
            if starved:
                return self._end(n, None, "nothing came from a receiver to listen to: is the radio playing it, and not paused?")
        if not x.any():
            return self._end(n, None, "the receiver is silent: nothing to go by (a squelched channel with nobody on it?)")
        if not self._own_process:
            try:
                return self._end(n, identify(x, hz, mode))
            except Exception as e:
                log.exception("identify: the naming failed")
                return self._end(n, None, f"it couldn't be worked out ({type(e).__name__})")
        ctx = mp.get_context("spawn")
        conn, child = ctx.Pipe()
        proc = ctx.Process(target=_think, args=(child, x, hz, mode), name="identify", daemon=True)
        try:
            proc.start()
            child.close()
            said = conn.recv() if conn.poll(THINK_S) else {"error": "it took too long to work out"}
        except (EOFError, OSError):
            said = {"error": "it couldn't be worked out"}
        finally:
            proc.join(timeout=1)
            if proc.is_alive():
                proc.kill()
            conn.close()
        self._end(n, None if "error" in said else said, said.get("error"))


listener = Listener()
