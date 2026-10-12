"""PSK31 and RTTY for receivers (audio/psk.py, audio/rtty.py, audio/keyboard.py)."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import ft8, keyboard, morse, pcm, psk, rtty
from sleepradiopi.web.server import make_handler

RATE = keyboard.RATE


def _noisy(x: np.ndarray, snr_db: float, seed: int = 1) -> np.ndarray:
    """x in noise: its strength against the noise in 2.5 kHz, as signal reports have it."""
    power = float(np.mean(x ** 2))
    return x + np.random.default_rng(seed).normal(0, np.sqrt(power / 10 ** (snr_db / 10) / 2500 * RATE / 2), len(x))


def _said(chars) -> str:
    return "".join(c[0] for c in chars)


def _followed(mode: str, x: np.ndarray) -> dict[int, str]:
    """Play x (at the reader's rate) to a Reader a step at a time. -> what it read, by pitch"""
    r = keyboard.Reader(mode)
    r.on = True                                      # (no thread: stepped here)
    step = int(RATE * keyboard.STEP_S)
    for i in range(0, len(x), step):
        r.take(x[i:i + step])
        r.step()
    lines: dict[int, str] = {}
    for p in r.status()["pieces"]:
        lines[p["hz"]] = lines.get(p["hz"], "") + p["text"]
    return lines


# --- PSK31 ---------------------------------------------------------------------------------

def test_varicode_is_whole() -> None:
    assert len(psk.VARICODE) == 128 and all("00" not in bits and bits[0] == bits[-1] == "1" for bits in psk.VARICODE)
    assert (psk.ENCODE[" "], psk.ENCODE["e"], psk.ENCODE["t"], psk.ENCODE["\n"]) == ("1", "11", "101", "11101")


@pytest.mark.parametrize("pitch", [600.0, 1503.7, 2700.0])
def test_psk31_is_read_down_into_the_noise(pitch: float) -> None:
    text = "CQ CQ CQ de M8ODJ M8ODJ pse k\n"
    for snr in (10, -6, -9):                         # (against the noise in 2.5 kHz: it's only 31 Hz wide)
        x = _noisy(psk.send(text, RATE, pitch) * 3000, snr)
        r = psk.read(x, RATE, pitch + 3.0)           # (from a few Hz off: it finds the rest)
        assert _said(r["chars"]) == text and abs(r["pitch"] - pitch) < 1.0, (pitch, snr)
    found = psk.find(_noisy(psk.send(text, RATE, pitch) * 3000, -6), RATE)
    assert found and abs(found[0][0] - pitch) < 5.0


@pytest.mark.parametrize("pitch", [600.0, 1503.7, 2700.0])
def test_psk63_is_read_too_and_neither_speed_reads_the_other(pitch: float) -> None:
    text = "CQ TEST de G4XYZ G4XYZ 599 001 The quick brown fox, jumps over 0123456789 times?\n"
    for snr in (10, -3, -6):                         # (twice as wide as PSK31: 3 dB less to spare)
        x = _noisy(psk.send(text, RATE, pitch, baud=62.5) * 3000, snr)
        r = psk.read(x, RATE, pitch + 5.0, 62.5)
        assert _said(r["chars"]) == text and abs(r["pitch"] - pitch) < 1.0 and r["baud"] == 62.5, (pitch, snr)
        assert psk.read(x, RATE, pitch + 5.0)["chars"] == [], (pitch, snr)            # read as PSK31: nothing, not rubbish
    x = _noisy(psk.send(text, RATE, pitch) * 3000, -3)
    assert psk.read(x, RATE, pitch + 2.0, 62.5)["chars"] == [] and _said(psk.read(x, RATE, pitch + 2.0)["chars"]) == text      # ...and PSK31 as PSK63
    rng = np.random.default_rng(4)                   # noise, and a carrier, at the faster speed: nothing
    assert psk.read(rng.normal(0, 1000, int(RATE * 8)), RATE, pitch, 62.5)["chars"] == []
    tone = 3000 * np.cos(2 * np.pi * pitch * np.arange(int(RATE * 8)) / RATE) + rng.normal(0, 300, int(RATE * 8))
    assert psk.read(tone, RATE, pitch, 62.5)["chars"] == [] and psk.read(tone, RATE, pitch + 31.0, 62.5)["chars"] == []


def test_psk31_and_psk63_side_by_side_each_at_its_own_speed() -> None:
    rng = np.random.default_rng(5)
    said = {800: (31.25, "CQ CQ de M8ODJ M8ODJ pse k\n" * 2), 1500: (62.5, "CQ TEST de G4XYZ G4XYZ test 599 001 001 the quick brown fox\n" * 2),
            2200: (31.25, "DL1ABC de F5ABC ur 599 599 name is Jean\n" * 2)}
    n = int(RATE * 30)
    x = rng.normal(0, 300, n)
    for pitch, (baud, text) in said.items():
        y = psk.send(text, RATE, pitch, baud=baud) * 1500
        x[int(2 * RATE):int(2 * RATE) + len(y)] += y[:n - int(2 * RATE)]
    r = keyboard.Reader("psk31")
    r.on = True
    step, speeds = int(RATE * keyboard.STEP_S), {}
    for i in range(0, len(x), step):
        r.take(x[i:i + step])
        r.step()
        speeds.update({s["hz"]: s["baud"] for s in r.status()["signals"]})
    lines: dict[int, str] = {}
    for p in r.status()["pieces"]:
        lines[p["hz"]] = lines.get(p["hz"], "") + p["text"]
        assert p["baud"] == psk.NAMES[said[min(said, key=lambda hz: abs(hz - p["hz"]))][0]]
    assert sorted(lines) == sorted(said) and all(lines[hz] == said[hz][1] for hz in said)
    assert speeds == {800: 31, 1500: 63, 2200: 31}


# --- QPSK31 ----------------------------------------------------------------------------------

def test_the_qpsk_code_is_g3plxs() -> None:
    # (rate 1/2, five bits deep, the polynomials 0x17 and 0x19; bit 0 of what comes out from the first. Noughts all
    # the way are half turns, as PSK31's idle is, and ones no turn at all.)
    assert list(psk._QPSK_OUT[:8]) == [0, 3, 1, 2, 1, 2, 0, 3] and psk._QPSK_OUT[16] == 3 and psk._QPSK_OUT[31] == 2
    assert psk._QPSK_TURN[psk._QPSK_OUT[0]] == -1 and psk._QPSK_TURN[psk._QPSK_OUT[31]] == 1
    # the bits a run of turns came from are found again, and a few turns spoilt are put right from those around them
    rng = np.random.default_rng(1)
    bits = rng.integers(0, 2, 200)
    sr, turns = 0, []
    for b in bits:
        sr = ((sr << 1) | int(b)) & 31
        turns.append(psk._QPSK_TURN[psk._QPSK_OUT[sr]])
    turns = np.array(turns)
    got, agree = psk.qpsk_bits(turns)
    assert (got[4:] == bits[4:]).all() and agree[4:].all()
    spoilt = turns.copy()
    for i in (30, 61, 95, 140):
        spoilt[i] *= 1j
    got, agree = psk.qpsk_bits(spoilt)
    assert (got[4:] == bits[4:]).all() and not agree[[30, 61, 95, 140]].any() and agree.sum() == 196


@pytest.mark.parametrize("pitch", [600.0, 1503.7, 2700.0])
def test_qpsk31_is_read_down_into_the_noise_and_only_the_right_way_up(pitch: float) -> None:
    text = "CQ CQ CQ de M8ODJ M8ODJ pse k\n"
    for snr in (10, -6, -9):
        x = _noisy(psk.qpsk_send(text, RATE, pitch) * 3000, snr)
        r = psk.read_qpsk(x, RATE, pitch + 1.5)      # (from a little off: it finds the rest)
        assert _said(r["chars"]) == text and abs(r["pitch"] - pitch) < 1.0 and r["qpsk"] and r["eye"] > 0.9, (pitch, snr)
        assert psk.read_qpsk(x, RATE, pitch + 1.5, reverse=True)["chars"] == []          # the other sideband's way: nothing
        assert len(psk.read(x, RATE, pitch + 1.5)["chars"]) <= 1, (pitch, snr)           # ...and as plain PSK31, nothing to speak of
    x = _noisy(psk.qpsk_send(text, RATE, pitch, reverse=True) * 3000, 0)                # heard in lower sideband
    assert psk.read_qpsk(x, RATE, pitch - 1.5)["chars"] == [] and _said(psk.read_qpsk(x, RATE, pitch - 1.5, reverse=True)["chars"]) == text
    assert abs(psk.read_qpsk(x, RATE, pitch - 1.5, reverse=True)["pitch"] - pitch) < 1.0


def test_qpsk31_says_nothing_of_psk31_noise_or_a_carrier() -> None:
    rng = np.random.default_rng(3)
    n = int(RATE * 8)
    for seed in range(6):
        for reverse in (False, True):
            assert psk.read_qpsk(np.random.default_rng(seed).normal(0, 1000, n), RATE, 1000.0, reverse=reverse)["chars"] == []
    for snr in (10, -3):                             # plain PSK31: every turn is one of QPSK's four, but they don't make its code
        x = _noisy(psk.send("CQ CQ de G4XYZ G4XYZ the quick brown fox 599\n", RATE, 1000.0) * 3000, snr)
        assert psk.read_qpsk(x, RATE, 1001.0)["chars"] == [] and psk.read_qpsk(x, RATE, 1001.0, reverse=True)["chars"] == []
    tone = 3000 * np.cos(2 * np.pi * 1200 * np.arange(n) / RATE) + rng.normal(0, 300, n)
    for off in (0.0, 4.0, 7.8, 15.6):                # a whistle, on pitch and at each place its turning looks like one of the four
        assert psk.read_qpsk(tone, RATE, 1200.0 + off)["chars"] == [], off


def test_qpsk31_among_the_others_each_read_as_what_it_is() -> None:
    rng = np.random.default_rng(5)
    said = {800: ("psk31", "CQ CQ de M8ODJ M8ODJ pse k\n" * 2), 1500: ("qpsk", "CQ de G4XYZ G4XYZ qpsk test 599 the quick brown fox\n" * 2),
            2200: ("qpsk-lsb", "DL1ABC de F5ABC ur 599 name is Jean\n" * 2)}
    n = int(RATE * 34)
    x = rng.normal(0, 300, n)
    for pitch, (kind, text) in said.items():
        y = (psk.send(text, RATE, pitch) if kind == "psk31" else psk.qpsk_send(text, RATE, pitch, reverse=kind == "qpsk-lsb")) * 1500
        x[int(2 * RATE):int(2 * RATE) + len(y)] += y[:n - int(2 * RATE)]
    r = keyboard.Reader("psk31")
    r.on = True
    step, kinds = int(RATE * keyboard.STEP_S), {}
    for i in range(0, len(x), step):
        r.take(x[i:i + step])
        r.step()
        kinds.update({s["hz"]: bool(s.get("qpsk")) for s in r.status()["signals"]})
    lines: dict[int, str] = {}
    for p in r.status()["pieces"]:
        lines[p["hz"]] = lines.get(p["hz"], "") + p["text"]
        assert bool(p.get("qpsk")) == (said[min(said, key=lambda hz: abs(hz - p["hz"]))][0] != "psk31") and p["baud"] == 31
    assert sorted(lines) == sorted(said) and all(lines[hz] == said[hz][1] for hz in said)
    assert kinds == {800: False, 1500: True, 2200: True}


def test_psk31_says_nothing_of_noise_or_a_carrier() -> None:
    rng = np.random.default_rng(4)
    for _ in range(5):
        x = rng.normal(0, 1000, int(RATE * 8))
        assert psk.find(x, RATE) == []
    t = np.arange(int(RATE * 30))
    for off in (0.0, 8.0, 15.6, 20.0):               # a whistle: on pitch it never turns; half the symbol rate off it "turns" every time
        x = 1500 * np.cos(2 * np.pi * (1000 + off) * t / RATE) + rng.normal(0, 600, len(t))
        assert _followed("psk31", x) == {}, off


def test_psk31_all_of_them_at_once_as_they_come() -> None:
    sent = (("the quick brown fox jumps over the lazy dog 0123456789 the quick brown fox jumps over the lazy dog", 700, 3000, 0.0),
            ("M0ABC de G4XYZ ur rst 599 599 name is Bob Bob qth York York hw? M0ABC de G4XYZ k", 1210, 800, 3.0),
            ("close by: CQ CQ CQ de ZL1AA ZL1AA ZL1AA pse k", 1290, 2000, 9.0))
    n = int(RATE * 40)
    x = np.random.default_rng(3).normal(0, 300, n)
    for text, pitch, level, at in sent:
        y, i = psk.send(text, RATE, pitch) * level, int(at * RATE)
        x[i:i + len(y)] += y[:n - i]
    got = _followed("psk31", x)                      # read a step at a time: nothing twice at the joins, nothing after one stops
    assert {hz: text.strip() for hz, text in got.items()} == {pitch: text for text, pitch, _, _ in sent}


# --- RTTY ----------------------------------------------------------------------------------

MESSAGE = "CQ CQ CQ DE M8ODJ M8ODJ M8ODJ PSE K\r\nRYRYRY 599 001 001 TU 73 = (WX) 12:30?\r\n"


@pytest.mark.parametrize("shift, baud, mark_high, low", [(170, 45.45, True, 915), (170, 45.45, False, 2125), (450, 50.0, False, 1275), (850, 75.0, True, 1000)])
def test_rtty_is_read_at_each_speed_and_either_way_up(shift: float, baud: float, mark_high: bool, low: float) -> None:
    for snr in (10, -2):
        x = _noisy(rtty.send(MESSAGE, RATE, low, shift, baud, mark_high) * 3000, snr)
        found = rtty.find(x, RATE)
        assert found and abs(found[0] - low) < 6 and found[1] == shift, (shift, snr, found)
        r = rtty.read(x, RATE, found[0], found[1])
        chars, figs = rtty.text(r["codes"])
        assert (r["baud"], r["mark_high"]) == (baud, mark_high) and r["framed"] > 0.95
        assert _said(chars) == MESSAGE and figs is True, (shift, snr)      # (it ended in figures: a new line doesn't shift back)


def test_rtty_says_nothing_of_noise() -> None:
    rng = np.random.default_rng(6)
    for _ in range(10):
        x = rng.normal(0, 1000, int(RATE * 8))
        assert rtty.read(x, RATE, 915.0, 170.0)["codes"] == []
    assert _followed("rtty", rng.normal(0, 1000, int(RATE * 40))) == {}


def test_two_steady_tones_are_not_a_teleprinter() -> None:
    # (two carriers 450 Hz apart on 14.090 MHz, with noise deciding between them, framed now and then and read as
    # "KKHXK8'QQCKBU": a teleprinter sends one tone or the other, never both)
    rng = np.random.default_rng(11)
    t = np.arange(int(RATE * 8)) / RATE
    said = 0
    for seed in range(12):
        x = 1500 * np.sin(2 * np.pi * 1111 * t) + 1500 * np.sin(2 * np.pi * 1561 * t) + np.random.default_rng(seed).normal(0, 900, len(t))
        r = rtty.read(x, RATE, 1111.0, 450.0)
        said += len(r["codes"])
        assert r["both"] > rtty.BOTH_MAX or not r["codes"]
    assert said == 0
    for snr in (10, -3):                             # a real one, strong or weak, is one tone at a time
        y = _noisy(rtty.send(MESSAGE, RATE, 915, 170, 45.45, True) * 2000, snr)
        r = rtty.read(y, RATE, 915.0, 170.0)
        assert r["codes"] and r["both"] < 0.3, snr
    assert rng is not None


def test_rtty_followed_from_its_start_to_its_last_letter() -> None:
    rng = np.random.default_rng(7)
    y = rtty.send(MESSAGE * 3, RATE, 915, 170, 45.45, True) * 2000
    x = np.concatenate((rng.normal(0, 300, int(RATE * 4)), y + rng.normal(0, 300, len(y)), rng.normal(0, 300, int(RATE * 6))))
    got = _followed("rtty", x)                       # read a step at a time: the figures shift carried across the joins
    (hz, text), = got.items()                        # (one signal, named by the middle of its two tones)
    assert abs(hz - 1000) <= 4 and text.strip() == (MESSAGE * 3).replace("\r", "").strip()


def test_rtty_several_at_once_as_in_a_contest() -> None:
    # four side by side, of different strengths, one the other way up, each starting when it likes
    msgs = ["CQ TEST DE G4XYZ G4XYZ CQ\r\n" * 3, "DL1ABC 599 023 023 TU\r\n" * 4, "F5ABC DE EA3XYZ 599 114 114 K\r\n" * 3, "RYRYRY DE OH2ABC OH2ABC 599 = 12:30\r\n" * 3]
    rng = np.random.default_rng(6)
    n = int(RATE * 40)
    x = rng.normal(0, 300, n)
    for (low, amp, high, delay), m in zip([(500, 2000, True, 2.0), (950, 1200, True, 5.0), (1400, 900, False, 1.0), (1850, 1500, True, 8.0)], msgs):
        y = rtty.send(m, RATE, low, 170, 45.45, high) * amp
        x[int(delay * RATE):int(delay * RATE) + len(y)] += y[:n - int(delay * RATE)]
    found = rtty.find_all(x[int(10 * RATE):int(18 * RATE)], RATE, most=8)       # (all four going)
    assert sorted(round(low / 10) * 10 for low, shift, _ in found if shift == 170.0)[:4] == [500, 950, 1400, 1850]
    got = _followed("rtty", x)
    assert [abs(hz - want) <= 6 for hz, want in zip(sorted(got), (585, 1035, 1485, 1935))] == [True] * 4
    # each from its first letter to its last, and nothing after it stopped (next door's keying leaks into its tones)
    assert [got[hz].strip() for hz in sorted(got)] == [m.replace("\r", "").strip() for m in msgs]


def test_rtty_of_different_shifts_and_speeds_together() -> None:
    rng = np.random.default_rng(9)
    n = int(RATE * 30)
    x = rng.normal(0, 300, n)
    said = [(600, 170, 45.45, "CQ CQ DE M8ODJ M8ODJ K\r\n" * 3), (1275, 450, 50.0, "ZCZC 123 WEATHER FORECAST NORTH SEA = WIND 5 TO 7\r\n" * 3)]
    for low, shift, baud, m in said:
        y = rtty.send(m, RATE, low, shift, baud, True) * 1500
        x[int(2 * RATE):int(2 * RATE) + len(y)] += y[:n - int(2 * RATE)]
    got = _followed("rtty", x)
    assert len(got) == 2
    for (hz, text), (low, shift, baud, m) in zip(sorted(got.items()), said):
        assert abs(hz - (low + shift / 2)) <= 6 and m.replace("\r", "").strip() in text.strip() and len(text.strip()) <= len(m.replace("\r", "").strip()) + 2
    assert _followed("rtty", rng.normal(0, 300, int(RATE * 40))) == {}           # and of noise alone, nothing


def test_a_wide_teleprinter_is_one_signal_not_a_narrow_one_inside_it() -> None:
    # (the German weather service read off the air as two: its own, and a "170 Hz" pair made of the energy its
    # keying puts between its tones, which framed and read as rubbish)
    r = keyboard.Reader("rtty")
    wide = {"low": 860.0, "shift": 450.0, "baud": 50.0, "mark_high": False, "done": 0.0, "last": 0.0, "figs": False, "framed": 1.0, "level": 1.0}
    narrow = {**wide, "low": 1000.0, "shift": 170.0}
    rng = np.random.default_rng(2)
    y = rtty.send("ZCZC WEATHER FORECAST NORTH SEA = WIND 5 TO 7\r\n" * 3, RATE, 860, 450, 50.0, False) * 2000
    x = (y + rng.normal(0, 300, len(y)))[:int(RATE * 8)].astype(np.float32)
    real = rtty.find_all
    try:
        rtty.find_all = lambda x, rate, most=4: [(1000.0, 170.0, 20.0), (860.0, 450.0, 30.0)]       # (both offered, the narrow one first)
        r._rttys = [dict(wide, level=rtty.read(x, RATE, 860.0, 450.0)["level"])]
        r._teleprinter(x, 0.0, 8.0)                  # the wide one being read: the pair inside it isn't even tried
        assert [(s["low"], s["shift"]) for s in r._rttys] == [(860.0, 450.0)] and r._tried == {}
        r._rttys = [dict(narrow)]                    # the narrow one got in first: the wide one frames, and takes over
        pieces, signals = r._teleprinter(x, 0.0, 8.0)
        assert [(s["low"], s["shift"]) for s in r._rttys] == [(860.0, 450.0)] and len(signals) == 1
        assert "WEATHER FORECAST" in "".join(p["text"] for p in pieces)
    finally:
        rtty.find_all = real


# --- the reader as the radio has it -----------------------------------------------------------

def test_the_switch_and_the_process_of_its_own() -> None:
    r = keyboard.Remote()
    assert not r.on and r.mode == "psk31"
    text = "read off the air 73"                     # (short: it's all fed at once, and only the last few seconds are looked at)
    x = np.concatenate((psk.send(text, RATE, 1000.0) * 3000, np.zeros(int(RATE * 3))))
    x = x + np.random.default_rng(2).normal(0, 200, len(x))
    played = np.repeat(np.repeat(np.clip(x, -32768, 32767), keyboard.DECIMATE).astype(np.int16)[:, None], pcm.CHANNELS, axis=1)
    r.switch(True)
    try:
        with pytest.raises(ValueError):
            r.switch(True, "hellschreiber")
        for i in range(0, len(played), 4410):        # (as the station plays it, but all at once: the reader isn't told the time)
            r.feed(played[i:i + 4410])
        until = time.time() + 20
        while time.time() < until and text not in "".join(p["text"] for p in r.status()["pieces"]):
            time.sleep(0.2)
        st = r.status()
        assert text in "".join(p["text"] for p in st["pieces"]) and st["on"] and st["mode"] == "psk31"
        assert r.status(since=st["pieces"][-1]["n"])["pieces"] == []
        r.switch(True, "rtty")                       # the other mode: from nothing
        assert r.mode == "rtty" and r.status()["pieces"] == []
    finally:
        r.switch(False)
    assert not r.on


def test_the_reader_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/rx/keyboard")[1]["on"] is False
        assert call("/api/rx/keyboard", {"on": True}) == (200, {"cw": False, "ft8": None, "keyboard": "psk31", "pictures": None}) and keyboard.reader.on
        assert call("/api/rx/keyboard", {"on": True, "mode": "rtty"})[1]["keyboard"] == "rtty" and keyboard.reader.mode == "rtty"
        for bad in ({"on": "yes"}, {"on": True, "mode": "ft8"}, {}):
            assert call("/api/rx/keyboard", bad)[0] == 400 and keyboard.reader.mode == "rtty"
        code, st = call("/api/rx/keyboard?since=3")
        assert code == 200 and st["on"] is True and st["mode"] == "rtty" and st["pieces"] == [] and st["signals"] == []
        # one reader at a time: each mode has its own place on a band
        decoder = tmp_path / "decode_ft8"
        decoder.write_text("#!/bin/sh\n")
        decoder.chmod(0o755)
        monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(decoder))
        assert call("/api/rx/ft8", {"on": True, "mode": "ft8"}) == (200, {"cw": False, "ft8": "ft8", "keyboard": None, "pictures": None}) and not keyboard.reader.on
        assert call("/api/rx/morse", {"on": True}) == (200, {"cw": True, "ft8": None, "keyboard": None, "pictures": None}) and not ft8.reader.on
        assert call("/api/rx/keyboard", {"on": True, "mode": "psk31"}) == (200, {"cw": False, "ft8": None, "keyboard": "psk31", "pictures": None}) and not morse.reader.on
        assert call("/api/rx/keyboard", {"on": False})[1]["keyboard"] is None and not keyboard.reader.on
    finally:
        keyboard.reader.switch(False)
        ft8.reader.switch(False)
        morse.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()


def test_receivers_are_given_names(tmp_path: Path) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/rx/name") == (200, {"receiver_names": {}})
        hf, vhf = "http://192.168.50.4:8075", "http://192.168.50.4:8074"       # two on one computer: only the port to tell them by
        assert call("/api/rx/name", {"base": hf, "name": "  Short   wave "}) == (200, {"receiver_names": {hf: "Short wave"}})
        assert call("/api/rx/name", {"base": vhf, "name": "VHF"})[1]["receiver_names"] == {hf: "Short wave", vhf: "VHF"}
        assert json.loads(conf.read_text())["receiver_names"] == {hf: "Short wave", vhf: "VHF"}          # kept
        assert call("/api/rx/name", {"base": hf, "name": "x" * 50})[1]["receiver_names"][hf] == "x" * 30
        assert call("/api/rx/name", {"base": hf, "name": ""})[1]["receiver_names"] == {vhf: "VHF"}      # its own name again
        for bad in ({}, {"name": "VHF"}, {"base": "", "name": "VHF"}, {"base": "http://a b", "name": "VHF"}):
            assert call("/api/rx/name", bad)[0] == 400
        assert call("/api/rx/name")[1] == {"receiver_names": {vhf: "VHF"}}
    finally:
        httpd.shutdown()
        httpd.server_close()
