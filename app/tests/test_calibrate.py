"""A receiver of your own put on frequency (playback/calibrate.py)."""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import rtty
from sleepradiopi.playback import calibrate
from sleepradiopi.web.server import make_handler

RATE = calibrate.AUDIO_RATE
SHIFT = 125_000_000


def _carrier(pitch: float, seconds: float = 1.6, level: float = 3000.0, noise: float = 300.0, seed: int = 1) -> np.ndarray:
    """A broadcast station tuned 1 kHz under: its carrier as a tone, a programme either side of it, and noise."""
    t = np.arange(int(seconds * RATE)) / RATE
    rng = np.random.default_rng(seed)
    speech = sum(0.15 * np.cos(2 * np.pi * (pitch + f) * t + rng.uniform(0, 6)) for f in (-431.0, 187.0, 523.0, 911.0))
    return (level * (np.cos(2 * np.pi * pitch * t) + speech) + rng.normal(0, noise, len(t))).astype(np.float32)


def test_a_carrier_is_found_to_a_hz() -> None:
    for pitch in (1000.0, 912.4, 1340.7, 310.0):
        assert abs(calibrate.tone(_carrier(pitch)) - pitch) < 1.0, pitch
    assert calibrate.tone(np.random.default_rng(2).normal(0, 1000, int(1.6 * RATE))) is None          # noise: nothing
    assert calibrate.tone(_carrier(1900.0)) is None and calibrate.tone(_carrier(1000.0)[:1000]) is None    # too far off; too short
    assert abs(calibrate.tone(_carrier(2000.0), near=2000.0) - 2000.0) < 1.0


def test_the_weather_teleprinters_middle() -> None:
    x = rtty.send("CQ CQ CQ DE DDK9 DDH7 DDH47 RYRYRYRY " * 2, RATE, 1275.0 + 37.0, 450.0, 50.0, False)[:3 * RATE] * 3000
    x = x + np.random.default_rng(3).normal(0, 300, len(x))
    assert abs(calibrate.weather(x) - 1537.0) < 4.0
    assert calibrate.weather(np.random.default_rng(4).normal(0, 1000, 3 * RATE)) is None
    amateur = rtty.send("CQ CQ CQ DE M8ODJ " * 3, RATE, 915.0, 170.0, 45.45, True)[:3 * RATE] * 3000        # (not the weather service: left alone)
    assert calibrate.weather(amateur) is None


def _row(peaks: dict[int, int], centre: int, rate: int = 256_000, n: int = 1024) -> bytes:
    row = np.full(n, 60, np.uint8)                   # (2.5 steps a dB: 60 is the noise)
    for hz, db in peaks.items():
        row[int(round((hz - centre) / rate * n + n / 2))] = 60 + int(db * 2.5)
    return row.tobytes()


def test_the_strongest_broadcast_carriers_in_a_waterfall() -> None:
    centre = 7_324_000
    rows = [_row({7_265_130: 40, 7_390_050: 30, 7_300_000: 12, 7_198_000: 50, centre: 55, 7_270_200: 35}, centre)] * 4
    # the strongest in the band, on their 5 kHz places: not the one outside it, the dongle's own centre, a weak one, or a neighbour
    assert calibrate.carriers(rows, centre, 256_000, 2.5, 7_200_000, 7_450_000) == [7_265_000, 7_390_000]
    assert calibrate.carriers(rows, centre, 256_000, 2.5, 7_200_000, 7_450_000, most=1) == [7_265_000]
    assert calibrate.carriers([], centre, 256_000, 2.5, 7_200_000, 7_450_000) == []


def test_what_the_measurements_ask_for() -> None:
    # as measured on the owner's receiver (ppm 67, trim 218): low at 6 MHz, right at 15, a line between
    points = [(6_130_000, -82.6), (7_265_000, -50.8), (7_646_000, -65.0), (9_460_000, -30.6), (9_665_000, -32.9), (9_710_000, -30.1),
              (11_650_000, -11.7), (11_725_000, -11.4), (11_875_000, -12.0), (13_760_000, -14.7), (13_830_000, 3.0), (15_200_000, -2.9),
              (15_550_000, -2.7), (17_715_000, 19.0), (15_240_000, -463.2)]
    r = calibrate.fit(points, 67, 218, SHIFT)
    assert (r["ppm"], r["trim"]) == (59, -897) and 7.0 < r["slope"] < 8.2
    assert r["left_out"] == [(15_240_000, -463.2)] and len(r["used"]) == 14 and r["was"] == 82.6 and r["left"] < 20      # (a station off its frequency)
    # the same stations with that set: nothing more to do
    after = [(f, e - 8e-6 * (SHIFT + f) + 1115) for f, e in points[:-1]]
    again = calibrate.fit(after, 59, -897, SHIFT)
    assert again["ppm"] == 59 and abs(again["trim"] + 897) <= 3
    # few stations, or all in one band: the slope can't be told, so only the trim is moved
    few = calibrate.fit([(9_460_000, 120.0), (9_665_000, 131.0), (9_710_000, 126.0)], 67, 218, SHIFT)
    assert (few["ppm"], few["trim"], few["slope"]) == (67, 218 + 126, None)
    for none in ([], [(9_460_000, 120.0)], [(9_460_000, 120.0), (9_710_000, -300.0)]):
        with pytest.raises(ValueError):
            calibrate.fit(none, 67, 218, SHIFT)
    with pytest.raises(ValueError):                  # (more than a dongle can be out: something else is wrong)
        calibrate.fit([(f, (f - 6e6) * 300e-6) for f in range(6_000_000, 18_000_000, 2_000_000)], 0, 0, SHIFT)


class _Receiver:
    """A short-wave receiver of our own, out by so many ppm and Hz: /status, /tune, /spectrum, /calibrate and its sound."""
    STATIONS = (6_130_000, 7_265_000, 9_460_000, 9_710_000, 11_725_000, 11_875_000, 13_760_000, 15_550_000, 17_715_000)
    OFF_AIR = 15_245_250                             # (a carrier that isn't on the raster: 250 Hz out)

    def __init__(self, ppm_out: float, hz_out: float, tuned: dict | None = None) -> None:
        self.ppm, self.trim, self.ppm_out, self.hz_out = 67, 218, ppm_out, hz_out
        self.spec, self.posts = tuned, []

    def error(self, hz: float) -> float:
        """How far above where it should be a station at hz is heard, with what's set now."""
        return self.hz_out + self.ppm_out * 1e-6 * hz + (self.ppm - 67) * 1e-6 * (SHIFT + hz) - (self.trim - 218)

    def get(self, url: str) -> dict:
        if url.endswith("/status"):
            return {"upconverter": SHIFT, "ppm": self.ppm, "trim": self.trim, "tunable": [100_000, 60_000_000],
                    "receiving": self.spec, **({"tunes": True} if self.spec else {})}
        centre = self.spec["freq"] + 24_000
        peaks = {hz: 30 for hz in (*self.STATIONS, self.OFF_AIR) if abs(hz - centre) < 120_000}
        return {"centre": centre, "rate": 256_000, "per_db": 2.5, "n": 7, "rows": [[8, base64.b64encode(_row(peaks, centre)).decode()]] * 3}

    def post(self, url: str, body: dict) -> dict:
        self.posts.append((url.rsplit("/", 1)[1], dict(body)))
        if url.endswith("/calibrate"):
            self.ppm, self.trim = body["ppm"], body["trim"]
        else:
            self.spec = {"freq": body["freq"], "mode": body["mode"]}
        return self.get("x/status")

    def sound(self, url: str):
        rx = self
        if not rx.spec:
            rx.spec = {"freq": 7_300_000, "mode": "usb"}

        class Sound:
            def take(self, seconds: float, settle: float = 0.0) -> np.ndarray:
                dial = rx.spec["freq"]
                heard = [hz for hz in (*rx.STATIONS, rx.OFF_AIR) if 300 < hz - dial + rx.error(hz) < 2700]
                if not heard:
                    return np.random.default_rng(5).normal(0, 300, int(seconds * RATE)).astype(np.float32)
                return _carrier(heard[0] - dial + rx.error(heard[0]), seconds)

            def close(self) -> None:
                pass
        return Sound()


def _run(rx: _Receiver, monkeypatch) -> dict:
    monkeypatch.setattr(calibrate.time, "sleep", lambda s: None)
    job = calibrate.Job(get=rx.get, post=rx.post, sound=rx.sound)
    job.start("http://receiver:8074")
    with pytest.raises(ValueError):
        job.start("http://receiver:8074")            # (one at a time)
    job._thread.join(timeout=60)
    return job.status()


def test_a_receiver_is_put_on_frequency(monkeypatch) -> None:
    rx = _Receiver(ppm_out=7.6, hz_out=-130.0, tuned={"freq": 7_074_000, "mode": "usb"})
    assert abs(rx.error(6_130_000) + 83) < 2 and abs(rx.error(17_715_000) - 5) < 2          # as the owner's was: 83 Hz low at 6 MHz
    st = _run(rx, monkeypatch)
    r = st["result"]
    assert not st["running"] and "error" not in st and r["before"] == {"ppm": 67, "trim": 218}
    assert (rx.ppm, rx.trim) == (r["ppm"], r["trim"]) == (59, 218 - 1115 + round(-130 + 83 + 46.6)) or abs(rx.ppm - 59) == 0
    assert all(abs(rx.error(hz)) < 12 for hz in rx.STATIONS) and r["now"] < 12 and r["was"] > 70
    assert [p[0] for p in r["left_out"]] == [15_245_000]                 # the one off the raster didn't fit the line
    assert rx.posts[-1] == ("tune", {"freq": 7_074_000, "mode": "usb"})    # back where it was
    assert ("calibrate", {"ppm": rx.ppm, "trim": rx.trim}) in rx.posts and len(st["points"]) >= 8


def test_a_receiver_already_on_frequency_is_left_alone_and_too_little_heard_says_so(monkeypatch) -> None:
    rx = _Receiver(ppm_out=0.0, hz_out=0.3)
    st = _run(rx, monkeypatch)
    assert (rx.ppm, rx.trim) == (67, 218) and not any(p[0] == "calibrate" for p in rx.posts) and st["result"]["now"] == st["result"]["was"] < 2
    quiet = _Receiver(ppm_out=7.6, hz_out=-130.0)
    quiet.STATIONS, quiet.OFF_AIR = (9_460_000,), 0
    st = _run(quiet, monkeypatch)
    assert "too few stations" in st["error"] and (quiet.ppm, quiet.trim) == (67, 218) and "result" not in st
    vhf = _Receiver(0.0, 0.0)
    monkeypatch.setattr(vhf, "get", lambda url: {"ppm": 0, "trim": 0, "tunable": [24_000_000, 1_766_000_000]})
    assert "short-wave receiver" in _run(vhf, monkeypatch)["error"]


def test_calibrating_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    rx = _Receiver(ppm_out=7.6, hz_out=-130.0)
    monkeypatch.setattr(calibrate, "job", calibrate.Job(get=rx.get, post=rx.post, sound=rx.sound))
    monkeypatch.setattr(calibrate.time, "sleep", lambda s: None)

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/rx/calibrate") == (200, {"running": False})
        for bad in ({}, {"base": "192.168.50.154:8074"}, {"base": "http://a b"}, {"base": "http://receiver:8074", "ppm": 1.5, "trim": 0}):
            assert call("/api/rx/calibrate", bad)[0] == 400
        code, st = call("/api/rx/calibrate", {"base": "http://receiver:8074/"})
        assert code == 200 and st["base"] == "http://receiver:8074"
        calibrate.job._thread.join(timeout=60)
        st = call("/api/rx/calibrate")[1]
        assert not st["running"] and st["result"]["ppm"] == rx.ppm == 59
        assert call("/api/rx/calibrate", {"base": "http://receiver:8074", "ppm": 67, "trim": 218}) == (200, {"ppm": 67, "trim": 218})      # undone
        assert (rx.ppm, rx.trim) == (67, 218)
    finally:
        httpd.shutdown()
        httpd.server_close()
