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

from sleepradiopi.audio import ft8, rtty
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
    assert r["ppm"] == 59 and abs(r["trim"] + 897) <= 10 and 7.0 < r["slope"] < 8.2
    assert (15_240_000, -463.2) in r["left_out"] and len(r["used"]) >= 12 and r["was"] == 82.6 and r["left"] < 25      # (a station off its frequency)
    # a few sloppy transmitters, 40 Hz out one way, don't draw it after them (the average did: 40 Hz out on FT8)
    sloppy = points[:-1] + [(7_300_000, -50.8 + 45.0), (9_500_000, -31.0 + 40.0), (11_900_000, -12.0 + 50.0), (13_600_000, -8.0 + 42.0)]
    s = calibrate.fit(sloppy, 67, 218, SHIFT)
    assert s["ppm"] == 59 and abs(s["trim"] - r["trim"]) <= 8
    # the same stations with that set: nothing more to do
    after = [(f, e - 8e-6 * (SHIFT + f) + 1115) for f, e in points[:-1]]
    again = calibrate.fit(after, 59, -897, SHIFT)
    assert again["ppm"] == 59 and abs(again["trim"] + 897) <= 10
    # few stations, or all in one band: the slope can't be told, so only the trim is moved
    few = calibrate.fit([(9_460_000, 120.0), (9_665_000, 131.0), (9_710_000, 126.0)], 67, 218, SHIFT)
    assert (few["ppm"], few["trim"], few["slope"]) == (67, 218 + 126, None)
    for none in ([], [(9_460_000, 120.0)], [(9_460_000, 120.0), (9_710_000, -300.0)]):
        with pytest.raises(ValueError):
            calibrate.fit(none, 67, 218, SHIFT)
    # as heard one evening: the stations above 15 MHz all 440 Hz below the rest. A line through both asked for 47 ppm
    # and left the receiver 400 Hz out; now nothing is changed
    split = [(7_265_000, 260.6), (7_340_000, 253.5), (7_646_000, 286.9), (9_500_000, 274.1), (9_585_000, 273.3), (9_730_000, 275.1),
             (15_210_000, -129.7), (15_240_000, -130.6), (15_720_000, -140.5), (17_520_000, -182.2), (17_700_000, -182.7), (17_760_000, -187.3)]
    with pytest.raises(ValueError):
        calibrate.fit(split, 64, -338, SHIFT)
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


def _ft8_run(rx: _Receiver, monkeypatch, open_bands=(7_074_000, 10_136_000, 14_074_000, 18_100_000), ours_hears=5) -> dict:
    """A calibration with a KiwiSDR to compare FT8 with. Each receiver's "sound" is only a number saying whose it is
    (1000s: ours, 2000s: the KiwiSDR's), which 15 s slot of the clock it is (the tens) and which band (the units).
    The "decoder" gives the KiwiSDR five stations on a band that's open; ours the same ones moved by the receiver's
    error there -- all five, or only `ours_hears` of them, a different one each slot."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(calibrate.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(calibrate.time, "time", lambda: clock[0])
    monkeypatch.setattr(calibrate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(calibrate.ft8, "decoder_path", lambda: "/usr/bin/decode_ft8")
    carriers_sound = rx.sound("x")
    slot = lambda t: (np.floor(np.asarray(t) / 15.0) % 10).astype(int)

    class Sound:
        def mark(self) -> None:
            self.began = clock[0]

        def since_mark(self) -> np.ndarray:
            t = self.began + np.arange(int((clock[0] - self.began) * RATE)) / RATE
            return (1000 + 10 * slot(t) + calibrate.FT8_HZ.index(rx.spec["freq"])).astype(np.float32)

        def take(self, seconds: float, settle: float = 0.0) -> np.ndarray:
            return carriers_sound.take(seconds)      # (the broadcast stations, as in the tests above)

        def close(self) -> None:
            pass

    class Kiwi:
        def __init__(self, url: str) -> None:
            self.band, self.ended = calibrate.FT8_HZ.index(int(round(float(url.split("?f=")[1][:-3]) * 1000))), None

        def start(self) -> None:
            pass

        def read(self, timeout: float = 0.1) -> np.ndarray:
            t = clock[0] + np.arange(22_050) / 44_100
            clock[0] += 0.5
            return np.repeat((2000 + 10 * slot(t) + self.band).astype(np.float32)[:, None], 2, axis=1)

        def close(self) -> None:
            pass

    def decode(samples: np.ndarray, mode: str) -> list[dict]:
        code = int(round(float(np.median(samples))))
        hz = calibrate.FT8_HZ[code % 10] if code >= 1000 else 0
        if hz not in open_bands:
            return []
        ours, n = code < 2000, (code // 10) % 10
        said = [{"snr": 5.0, "dt": 0.5, "hz": 600 + 300 * k + (rx.error(hz) if ours else 0.0), "text": f"CQ K{k}ABC FN42"} for k in range(5)]
        return said if not ours or ours_hears >= 5 else [said[(n + j) % 5] for j in range(ours_hears)]

    job = calibrate.Job(get=rx.get, post=rx.post, sound=lambda url: Sound(), kiwi=Kiwi, decode=decode)
    if not rx.spec:
        rx.spec = {"freq": 7_300_000, "mode": "usb"}
    t0 = clock[0]
    job.start("http://receiver:8075", "http://kiwi:8073/")
    job._thread.join(timeout=60)
    return {**job.status(), "took": clock[0] - t0}


def test_ft8_against_a_kiwisdr_is_the_yardstick(monkeypatch) -> None:
    rx = _Receiver(ppm_out=7.6, hz_out=-130.0, tuned={"freq": 3_580_000, "mode": "usb"})
    st = _ft8_run(rx, monkeypatch)
    r = st["result"]
    assert "error" not in st and r["by"] == "ft8" and [p[0] for p in r["used"]] == [7_074_000, 10_136_000, 14_074_000, 18_100_000]
    assert rx.ppm == r["ppm"] == 59 and all(abs(rx.error(hz)) < 4 for hz in (7_074_000, 14_074_000, 18_100_000)) and r["now"] < 4
    assert not any(p[0] == "tune" and p[1]["freq"] in (7_264_000, 9_459_000) for p in rx.posts)       # (no round of the broadcast bands)
    assert rx.posts[-1] == ("tune", {"freq": 3_580_000, "mode": "usb"})


def test_a_receiver_that_hears_little_is_listened_to_for_longer(monkeypatch) -> None:
    """One slot on each band found no band at all one night on a long wire, where a minute of each found two."""
    rx = _Receiver(ppm_out=7.6, hz_out=-130.0, tuned={"freq": 3_580_000, "mode": "usb"})
    st = _ft8_run(rx, monkeypatch, ours_hears=1)     # one station a slot, a different one each time: a station only counts in the slot both heard it
    r = st["result"]
    assert r["by"] == "ft8" and len(r["used"]) == 4 and rx.ppm == 59 and all(abs(rx.error(hz)) < 4 for hz in (7_074_000, 14_074_000))
    quick = _ft8_run(_Receiver(ppm_out=7.6, hz_out=-130.0, tuned={"freq": 3_580_000, "mode": "usb"}), monkeypatch)
    assert st["took"] > quick["took"] + 4 * 25 and quick["took"] < 6 * 40      # ...and a good receiver, and the closed bands, aren't kept waiting


def test_with_too_few_bands_open_its_the_broadcast_stations(monkeypatch) -> None:
    rx = _Receiver(ppm_out=7.6, hz_out=-130.0)
    st = _ft8_run(rx, monkeypatch, open_bands=(7_074_000,))
    assert st["result"]["by"] == "stations" and any(p[0] == "tune" and p[1]["freq"] == 9_459_000 for p in rx.posts)


def test_three_bands_of_ft8_are_enough_for_the_slope() -> None:
    # as the radio measured the owner's receiver one night: 40 m +47, 20 m +19, 17 m +3
    points = [(7_074_000, 47.0), (14_074_000, 19.0), (18_100_000, 3.0)]
    assert calibrate.fit(points, 56, -844, SHIFT)["slope"] is None               # (three stations wouldn't be)
    r = calibrate.fit(points, 56, -844, SHIFT, line_from=calibrate.FT8_LINE_FROM)
    assert r["ppm"] == 60 and -4.5 < r["slope"] < -3.5 and r["left"] < 5


def test_one_band_of_ft8_is_better_than_stations_that_wont_do(monkeypatch) -> None:
    rx = _Receiver(ppm_out=0.0, hz_out=36.0)         # 36 Hz out everywhere; and no broadcast station to be heard but one
    rx.STATIONS, rx.OFF_AIR = (9_460_000,), 0
    st = _ft8_run(rx, monkeypatch, open_bands=(7_074_000,))
    r = st["result"]
    assert r["by"] == "ft8" and r["used"] == [[7_074_000, 36.0]] and (rx.ppm, rx.trim) == (67, 218 + 36) and abs(rx.error(14_074_000)) < 1


class _Job:
    """A calibration that takes as long as the test says."""

    def __init__(self) -> None:
        self.state, self.started = {"running": False}, []

    def status(self) -> dict:
        return dict(self.state)

    def start(self, base: str, kiwi: str | None = None) -> None:
        if self.state.get("running"):
            raise ValueError("a calibration is already running")
        self.started.append((base, kiwi))
        self.state = {"running": True, "base": base}

    def done(self, **result) -> None:
        self.state = {"running": False, "base": self.state["base"], **result}


def _keeper(plan: dict, hour: float):
    """A Keeper on a clock of ours (hours since a midnight), with what the radio is playing in on[0]."""
    job, on, at = _Job(), [None], [hour * 3600.0]
    k = calibrate.Keeper(job, playing=lambda: on[0], kiwi=lambda: "http://kiwi:8073/", clock=lambda: at[0], localtime=__import__("time").gmtime)
    k.set_plan(calibrate.clean_plan(plan))
    return k, job, on, at


def test_by_itself_at_night_when_nobody_is_listening() -> None:
    rx = "http://receiver:8075"
    k, job, on, at = _keeper({rx + "/": "night"}, hour=2.9)
    k.tick()
    assert job.started == []                         # not yet 3 o'clock
    at[0] = 3.1 * 3600
    on[0] = rx + "/stream?freq=7074000"              # ...and now somebody is listening to it: not while they are
    k.tick()
    assert job.started == []
    on[0] = "http://stream.example/radio4"           # gone on to something else
    at[0] += 600
    k.tick()
    assert job.started == [(rx, "http://kiwi:8073/")]
    k.tick()                                         # (still going: nothing more)
    job.done(result={"ppm": 60, "trim": -170, "before": {"ppm": 60, "trim": -166}, "was": 6.0, "now": 2.0, "by": "ft8"})
    k.tick()
    [entry] = k.status()["log"]
    assert (entry["base"], entry["way"], entry["ppm"], entry["trim"], entry["before"], entry["by"]) == (rx, "night", 60, -170, {"ppm": 60, "trim": -166}, "ft8")
    at[0] += 1800
    k.tick()
    assert len(job.started) == 1                     # once a night
    at[0] += 24 * 3600                               # the next night: again; one that fails is said
    k.tick()
    assert len(job.started) == 2
    job.done(error="the receiver isn't answering (timed out)")
    k.tick()
    assert "answering" in k.status()["log"][0]["error"] and len(k.status()["log"]) == 2
    at[0] = 30 * 3600                                # six in the morning: too late
    k2, job2, on2, at2 = _keeper({rx: "night"}, hour=6.0)
    k2.tick()
    assert job2.started == []


def test_by_itself_while_listening_once_its_warm_and_then_now_and_again() -> None:
    rx = "http://receiver:8075"
    k, job, on, at = _keeper({rx: "listening"}, hour=14.0)
    k.tick()
    assert job.started == []                         # nobody listening: left alone
    on[0] = rx + "/stream"
    k.tick()
    at[0] += calibrate.WARM_S - 60
    k.tick()
    assert job.started == []                         # not warm yet
    at[0] += 120
    k.tick()
    assert len(job.started) == 1
    job.done(result={"ppm": 60, "trim": -150, "before": {"ppm": 60, "trim": -166}, "was": 16.0, "now": 2.0, "by": "ft8"})
    k.tick()
    at[0] += calibrate.AGAIN_S - 300
    k.tick()
    assert len(job.started) == 1                     # not again so soon
    at[0] += 600
    k.tick()
    assert len(job.started) == 2
    job.done(result={"ppm": 60, "trim": -150, "before": {"ppm": 60, "trim": -150}, "was": 3.0, "now": 3.0, "by": "ft8"})
    k.tick()
    on[0] = None                                     # switched to something else and back: it warms again first
    at[0] += calibrate.AGAIN_S + 60
    k.tick()
    on[0] = rx + "/stream"
    k.tick()
    assert len(job.started) == 2
    at[0] += calibrate.WARM_S + 1
    k.tick()
    assert len(job.started) == 3
    job2 = _Job()                                    # one begun by hand is waited for, not joined
    k3, job3, on3, at3 = _keeper({rx: "listening"}, hour=14.0)
    job3.state = {"running": True, "base": "http://another:8074"}
    on3[0] = rx + "/stream"
    k3.tick()
    at3[0] += calibrate.WARM_S + 1
    k3.tick()
    assert job3.started == [] and k3.status()["log"] == []


def test_the_plan_is_checked() -> None:
    assert calibrate.clean_plan({"http://a:8075/": "night", "https://b": "listening"}) == {"http://a:8075": "night", "https://b": "listening"}
    for bad in ([], {"a:8075": "night"}, {"http://a b": "night"}, {"http://a": "weekly"}, {"http://a": None}):
        with pytest.raises(ValueError):
            calibrate.clean_plan(bad)


def test_nothing_is_reported_while_a_receiver_is_being_calibrated(monkeypatch) -> None:
    r = ft8.Reader()
    r.own, r.dial, r.playing, r.report = "http://receiver:8075", 14_074_000, (lambda: "http://receiver:8075/stream"), (lambda *a: None)
    assert r.reports() is True
    busy = _Job()
    busy.state = {"running": True, "base": "http://receiver:8075"}
    monkeypatch.setattr(calibrate, "job", busy)      # (it's being taken round the bands: not where the page said)
    assert r.reports() is False


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

    monkeypatch.setattr(calibrate, "keeper", calibrate.Keeper(calibrate.job))
    try:
        assert call("/api/rx/calibrate") == (200, {"running": False, "itself": {"plan": {}, "log": []}})
        # by itself: chosen a receiver at a time, kept in the settings
        for bad in ({"base": "http://receiver:8074", "itself": "weekly"}, {"base": "receiver", "itself": "night"}):
            assert call("/api/rx/calibrate", bad)[0] == 400
        code, st = call("/api/rx/calibrate", {"base": "http://receiver:8074/", "itself": "night"})
        assert code == 200 and st["itself"]["plan"] == {"http://receiver:8074": "night"} and not st["running"]
        assert call("/api/rx/calibrate", {"base": "http://other:8075", "itself": "listening"})[1]["itself"]["plan"] == {"http://receiver:8074": "night", "http://other:8075": "listening"}
        assert json.loads(conf.read_text())["calibrate_itself"] == {"http://receiver:8074": "night", "http://other:8075": "listening"}
        assert call("/api/rx/calibrate", {"base": "http://receiver:8074", "itself": "never"})[1]["itself"]["plan"] == {"http://other:8075": "listening"}
        assert call("/api/rx/calibrate", {"base": "http://other:8075", "itself": "never"})[1]["itself"]["plan"] == {}
        assert json.loads(conf.read_text())["calibrate_itself"] == {}
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
