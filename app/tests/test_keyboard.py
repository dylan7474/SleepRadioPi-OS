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

from sleepradiopi.audio import keyboard, pcm, psk, rtty
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


def test_rtty_followed_from_its_start_to_its_last_letter() -> None:
    rng = np.random.default_rng(7)
    y = rtty.send(MESSAGE * 3, RATE, 915, 170, 45.45, True) * 2000
    x = np.concatenate((rng.normal(0, 300, int(RATE * 4)), y + rng.normal(0, 300, len(y)), rng.normal(0, 300, int(RATE * 6))))
    got = _followed("rtty", x)                       # read a step at a time: the figures shift carried across the joins
    (hz, text), = got.items()                        # (one signal, named by the middle of its two tones)
    assert abs(hz - 1000) <= 4 and text.strip() == (MESSAGE * 3).replace("\r", "").strip()


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


def test_the_reader_from_the_web(tmp_path: Path) -> None:
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
        assert call("/api/rx/keyboard", {"on": True}) == (200, {"keyboard": "psk31"}) and keyboard.reader.on
        assert call("/api/rx/keyboard", {"on": True, "mode": "rtty"}) == (200, {"keyboard": "rtty"}) and keyboard.reader.mode == "rtty"
        for bad in ({"on": "yes"}, {"on": True, "mode": "ft8"}, {}):
            assert call("/api/rx/keyboard", bad)[0] == 400 and keyboard.reader.mode == "rtty"
        code, st = call("/api/rx/keyboard?since=3")
        assert code == 200 and st["on"] is True and st["mode"] == "rtty" and st["pieces"] == [] and st["signals"] == []
        assert call("/api/rx/keyboard", {"on": False}) == (200, {"keyboard": None}) and not keyboard.reader.on
    finally:
        keyboard.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()
