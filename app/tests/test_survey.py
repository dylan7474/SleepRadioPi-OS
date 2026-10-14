"""The survey run by the radio (playback/survey.py)."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import ft8, psk
from sleepradiopi.playback import calibrate, survey
from sleepradiopi.web.server import make_handler

RX = "http://receiver:8075"
T0 = 1_800_000_003.0                                 # three seconds into an FT8 slot
PLACES = [{"key": "ft8:40 m", "mode": "ft8", "band": "40 m", "hz": 7_074_000}, {"key": "ft8:20 m", "mode": "ft8", "band": "20 m", "hz": 14_074_000},
          {"key": "ft4:20 m", "mode": "ft4", "band": "20 m", "hz": 14_080_000}, {"key": "wspr:20 m", "mode": "wspr", "band": "20 m", "hz": 14_095_600},
          {"key": "psk31:20 m", "mode": "psk31", "band": "20 m", "hz": 14_070_000}, {"key": "wx:10100.8", "mode": "weather", "band": "10.1008 MHz", "hz": 10_099_300},
          {"key": "ft8:2 m", "mode": "ft8", "band": "2 m", "hz": 144_174_000}]


class _Radio:
    """A receiver that's tuned and listened to, on a clock of the test's: what's 'on the air' is by frequency."""
    HEARD = {7_074_000: [{"snr": 3.0, "dt": 0.5, "hz": 1000, "text": "CQ K1ABC FN42"}, {"snr": -5.0, "dt": 0.4, "hz": 1500, "text": "G4XYZ JA1VSL PM95"},
                         {"snr": 1.0, "dt": 0.5, "hz": 700, "text": "TNX BOB 73 GL"}],
             14_080_000: [{"snr": 0.0, "dt": 0.5, "hz": 900, "text": "CQ DL1ABC JO31"}],
             14_095_600: [{"snr": -20.0, "dt": 0.2, "hz": 1500, "text": "W9XYZ EN37 30", "call": "W9XYZ", "grid": "EN37", "dbm": 30}]}

    def __init__(self, receiving=None) -> None:
        self.at, self.freq, self.tuned, self.receiving, self.closed, self.asked = T0, None, [], receiving, False, []
        self.slots: list[tuple[str, float]] = []     # (mode, how long the sound given to the decoder was, s)

    def clock(self) -> float:
        return self.at

    def sleep(self, s: float) -> None:
        self.at += s

    def get(self, url: str) -> dict:
        # (as the real one says it: "tunes" only while someone is listening to it as a rig; idle, "receiving" is null)
        return {"tunable": [100_000, 30_000_000], "receiving": self.receiving, "upconverter": 125, **({"tunes": True} if self.receiving else {})}

    def post(self, url: str, body: dict) -> dict:
        self.freq = body["freq"]
        self.tuned.append((body["freq"], body["mode"]))
        return {}

    def sound(self, url: str):
        radio = self
        radio.asked.append(url)

        class Sound:
            began = radio.at

            def mark(self) -> None:
                self.began = radio.at

            def since_mark(self) -> np.ndarray:
                n = int((radio.at - self.began) * survey.AUDIO_RATE)
                if radio.freq == 14_070_000:         # PSK31: two signals standing out of the hiss
                    t = np.arange(n) / survey.AUDIO_RATE
                    return (3000 * np.sin(2 * np.pi * 1000 * t) + 3000 * np.sin(2 * np.pi * 1400 * t) + np.random.default_rng(1).normal(0, 60, n)).astype(np.float32)
                return np.zeros(n, np.float32)

            def close(self) -> None:
                radio.closed = True

        return Sound()

    def decode(self, samples: np.ndarray, mode: str) -> list[dict]:
        self.slots.append((mode, len(samples) / survey.AUDIO_RATE))
        return [dict(m) for m in self.HEARD.get(self.freq, [])]


def _job(radio: _Radio) -> survey.Job:
    return survey.Job(get=radio.get, post=radio.post, sound=radio.sound, decode=radio.decode, clock=radio.clock, sleep=radio.sleep)


def test_a_round_of_the_bands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(calibrate, "weather", lambda x: 1500.0 if len(x) else None)
    radio = _Radio(receiving={"freq": 9_500_000, "mode": "am"})
    job = _job(radio)
    file = tmp_path / "state" / "survey.json"
    job.keep_in(file)
    job.start(RX, survey.clean_places(PLACES))
    with pytest.raises(ValueError, match="already"):
        job.start(RX, PLACES)
    job._thread.join(30)
    st = job.status()
    assert st == {"running": False, "base": RX, "at": None, "i": 7, "of": 7, "why": "asked", "done": 6}
    found = job.found(RX)
    assert (found["ft8:40 m"]["n"], found["ft8:20 m"]["n"], found["ft4:20 m"]["n"], found["wspr:20 m"]["n"], found["psk31:20 m"]["n"], found["wx:10100.8"]["n"]) == (3, 0, 1, 1, 2, 1)
    assert 9200 < found["ft8:40 m"]["km"] < 9500 and "km" not in found["ft8:20 m"] and 5500 < found["wspr:20 m"]["km"] < 6500      # the farthest, by its locator
    assert "ft8:2 m" not in found                    # (beyond what this receiver reaches: left)
    # it went to each place it reaches, in upper sideband, and back where it was at the end
    assert radio.tuned == [(p["hz"], "usb") for p in PLACES[:6]] + [(9_500_000, "am")] and radio.closed
    assert radio.asked == [RX + "/audio"]            # (someone was listening: joined as it was)
    # a whole slot each, begun on the clock: FT8's 15 s, FT4's 7.5, WSPR's 114 of its two minutes
    assert sorted({(m, round(s, 1)) for m, s in radio.slots}) == [("ft4", 7.5), ("ft8", 15.0), ("wspr", 114.0)]
    assert json.loads(file.read_text())["found"][RX]["ft8:40 m"]["n"] == 3
    again = survey.Job(clock=radio.clock)            # the radio started again: what was found is still there
    again.keep_in(file)
    assert again.found(RX)["ft8:40 m"]["n"] == 3 and again.found("http://another:1") == {}
    again.forget(RX)
    assert again.found(RX) == {} and json.loads(file.read_text()) == {"found": {}}


def test_a_bands_day(monkeypatch) -> None:
    radio = _Radio()
    job = _job(radio)
    place = survey.clean_places(PLACES[:1])
    for n, hours in ((3, 0), (0, 3), (1, 9), (2, 30)):
        radio.HEARD = {7_074_000: [{"snr": 0.0, "dt": 0.5, "hz": 800 + i, "text": f"CQ G{i}ABC IO91"} for i in range(n)]}
        radio.at = T0 + hours * 3600
        job.start(RX, place, "itself")
        job._thread.join(30)
    f = job.found(RX)["ft8:40 m"]
    assert f["n"] == 2 and [d[1] for d in f["day"]] == [1, 2]          # the latest, and each finding of the last day (the older ones gone)
    assert radio.asked[0] == RX + "/audio?freq=7.0740&mode=usb"        # (nobody listening: started there)
    assert job.status()["why"] == "itself"


def test_a_round_can_be_stopped_and_a_receiver_that_wont_do_is_said() -> None:
    radio = _Radio(receiving={"freq": 9_500_000, "mode": "am"})
    job = _job(radio)
    stopped = []

    def sleep(s: float) -> None:                     # stop it while it's listening at the second place
        radio.at += s
        if len(radio.tuned) == 2 and not stopped:
            stopped.append(1)
            job.stop()
    job._sleep = sleep
    job.start(RX, survey.clean_places(PLACES))
    job._thread.join(30)
    st = job.status()
    assert not st["running"] and st["done"] == 1 and st["stopped"] is True and list(job.found(RX)) == ["ft8:40 m"]
    assert radio.tuned[-1] == (9_500_000, "am")      # put back all the same
    job._get = lambda url: {"receiving": None, "running": False}           # (a scanner of channels: nowhere it can be sent)
    job.start(RX, survey.clean_places(PLACES))
    job._thread.join(30)
    assert "can't move it" in job.status()["error"]

    def gone(url: str) -> dict:
        raise OSError("timed out")
    job._get = gone
    job.start(RX, survey.clean_places(PLACES))
    job._thread.join(30)
    assert "isn't answering" in job.status()["error"]


# --- the finder: a stretch of band swept for Morse and teleprinters ---------------------------

def _morse(text: str, wpm: float, pitch: float, seconds: float) -> np.ndarray:
    """A Morse tone at 12 kHz (amplitude 1), the text over and over for that long."""
    from sleepradiopi.audio.morse import MORSE
    CODE = {v: k for k, v in MORSE.items()}
    dit, key, t = 1.2 / wpm, np.zeros(int(seconds * survey.AUDIO_RATE)), 0.2
    while t < seconds:
        for ch in text:
            if ch == " ":
                t += 4 * dit
                continue
            for e in CODE[ch]:
                d = dit if e == "." else 3 * dit
                key[int(t * survey.AUDIO_RATE):int((t + d) * survey.AUDIO_RATE)] = 1.0
                t += d + dit
            t += 2 * dit
    return key * np.sin(2 * np.pi * pitch * np.arange(len(key)) / survey.AUDIO_RATE)


class _Band(_Radio):
    """A receiver with a waterfall, and a band with things on it: Morse, a teleprinter, FT4 coming and going, and
    the dongle's own steady whistle."""
    ON_AIR = {14_010_300: "cw", 14_025_600: "cw", 14_039_000: "whistle", 14_052_000: "carrier", 14_080_500: "ft4", 14_088_000: "rtty", 14_094_000: "tones"}

    def get(self, url: str) -> dict:
        if "/spectrum" not in url:
            return super().get(url)
        if url.endswith("since=0"):
            return {"running": True, "n": 100, "rows": []}
        import base64
        rng = np.random.default_rng(3)
        centre, n = 14_059_000, 1024                 # (not where it's tuned: the dongle's own centre, as the real one has it)
        f = centre + (np.arange(n) - n / 2) * 250.0
        rows = 30.0 + rng.normal(0, 0.4, (50, n))
        for hz, kind in self.ON_AIR.items():
            i = int(np.argmin(np.abs(f - hz)))
            rows[:, i] += {"cw": 18.0 * (rng.random(50) > 0.45), "ft4": 20.0 * (np.arange(50) % 30 < 18), "whistle": 22.0, "rtty": 15.0 + rng.normal(0, 1.2, 50),
                           "carrier": 6.0 + 14.0 * (rng.random(50) > 0.5), "tones": 14.0 + rng.normal(0, 1.5, 50)}[kind]
        return {"running": True, "n": 150, "centre": centre, "rate": 256_000, "per_db": 2.5,
                "rows": [[100 + k, base64.b64encode(np.clip(r * 2.5, 0, 255).astype(np.uint8).tobytes()).decode()] for k, r in enumerate(rows)]}

    def sound(self, url: str):
        radio = self
        radio.asked.append(url)

        class Sound:
            began = radio.at

            def mark(self) -> None:
                self.began = radio.at

            def since_mark(self) -> np.ndarray:
                seconds = radio.at - self.began
                n = int(seconds * survey.AUDIO_RATE)
                x = np.random.default_rng(5).normal(0, 100, n)
                for hz, kind in radio.ON_AIR.items():    # what of the band is in the 300 to 2900 Hz it hears, in upper sideband
                    pitch = hz - radio.freq
                    if not 300 <= pitch <= 2900:
                        continue
                    if kind == "cw":
                        x += 2000 * _morse("CQ CQ DE G4XYZ K ", 22, pitch, seconds)[:n]
                    elif kind in ("whistle", "carrier"):       # (a carrier with noise on it comes and goes, in a way, and isn't Morse)
                        x += 2000 * np.sin(2 * np.pi * pitch * np.arange(n) / survey.AUDIO_RATE) * (1.0 if kind == "whistle" else np.abs(np.convolve(np.random.default_rng(8).normal(0, 1, n), np.ones(300) / 300, "same")) * 25)
                    elif kind == "rtty":
                        from sleepradiopi.audio import rtty
                        x += 2000 * rtty.send("CQ CQ DE DL1ABC DL1ABC K\r\n" * 4, survey.AUDIO_RATE, pitch - 85, 170, 45.45, True, lead=0.1)[:n]
                    elif kind == "tones":            # two steady tones a teleprinter's distance apart, as on 14.090 MHz one afternoon
                        t = np.arange(n) / survey.AUDIO_RATE
                        x += 1500 * np.sin(2 * np.pi * (pitch - 225) * t) + 1500 * np.sin(2 * np.pi * (pitch + 225) * t)
                    else:                            # FT4-ish: a burst of tones, then nothing
                        t = np.arange(n) / survey.AUDIO_RATE
                        x += 2000 * np.sin(2 * np.pi * (pitch + 20 * np.floor(t * 20) % 4) * t) * (t % 3 < 1.8)
                return x.astype(np.float32)

            def close(self) -> None:
                radio.closed = True

        return Sound()


def test_what_comes_and_goes_in_a_waterfall() -> None:
    rows = np.array([np.frombuffer(__import__("base64").b64decode(r[1]), np.uint8) for r in _Band().get("http://r/spectrum?since=100")["rows"]], float) / 2.5
    cw = survey.active(rows, 14_059_000, 256_000, 14_000_000, 14_070_000, survey.SWING_DB["cw"])
    assert sorted(hz for hz, _, _ in cw) == [14_010_250, 14_025_500, 14_052_000]       # what comes and goes; not the whistle, as strong as any
    tele = survey.active(rows, 14_059_000, 256_000, 14_080_000, 14_100_000, survey.SWING_DB["rtty"])
    assert sorted(hz for hz, _, _ in tele) == [14_080_500, 14_088_000, 14_094_000]     # a teleprinter is steadier: all are worth a listen
    assert survey.active(rows, 14_059_000, 256_000, 14_053_000, 14_070_000, survey.SWING_DB["cw"]) == []
    assert survey.active(rows[:3], 14_059_000, 256_000, 14_000_000, 14_070_000, 5.0) == []     # (too short a look to tell)


def test_morse_and_teleprinters_are_found_by_sweeping_their_part_of_the_band(tmp_path: Path) -> None:
    radio = _Band(receiving={"freq": 14_074_000, "mode": "usb"})
    job = _job(radio)
    job.keep_in(tmp_path / "survey.json")
    places = survey.clean_places([{"key": "cw:20 m", "mode": "cw", "band": "20 m", "hz": 14_030_000, "lo": 14_000_000, "hi": 14_070_000},
                                  {"key": "rtty:20 m", "mode": "rtty", "band": "20 m", "hz": 14_085_000, "lo": 14_080_000, "hi": 14_100_000}])
    job.start(RX, places)
    job._thread.join(60)
    assert job.status()["done"] == 2 and "error" not in job.status()
    found = job.found(RX)
    cw, tele = found["cw:20 m"], found["rtty:20 m"]
    assert cw["n"] == 2 and sorted(round(s["hz"], -2) for s in cw["where"]) == [14_010_300, 14_025_600]      # where each is, to a hundred Hz
    assert all(19 <= s["wpm"] <= 25 for s in cw["where"])                      # ...and how fast, as the Morse reader read it
    assert tele["n"] == 1 and abs(tele["where"][0]["hz"] - 14_088_000) < 150 and (tele["where"][0]["baud"], tele["where"][0]["shift"]) == (45.45, 170)
    assert radio.tuned[0] == (14_035_000, "usb") and radio.tuned[-1] == (14_074_000, "usb")       # the middle of the stretch first; back at the end
    again = survey.Job(clock=radio.clock)            # kept, with where each was
    again.keep_in(tmp_path / "survey.json")
    assert again.found(RX)["cw:20 m"]["where"] == cw["where"]
    for bad in ({"key": "cw:20 m", "mode": "cw", "band": "20 m", "hz": 14_030_000}, {"key": "a", "mode": "rtty", "band": "x", "hz": 14e6, "lo": 14.1e6, "hi": 14.0e6},
                {"key": "a", "mode": "cw", "band": "x", "hz": 14e6, "lo": 14.0e6, "hi": 14.4e6}):
        with pytest.raises(ValueError):
            survey.clean_places([bad])



class _PskBand(_Radio):
    """17 m as it was on 2026-10-14: PSK31 below 18.100 MHz, and at 18.100 what stands out but doesn't spell (a carrier here)."""
    ON_AIR = {18_097_400: "cq cq de g4abc g4abc io94 pse k cq cq de g4abc g4abc io94 pse k ", 18_101_200: None}

    def sound(self, url: str):
        radio = self
        radio.asked.append(url)

        class Sound:
            began = radio.at

            def mark(self) -> None:
                self.began = radio.at

            def since_mark(self) -> np.ndarray:
                n = int((radio.at - self.began) * survey.AUDIO_RATE)
                x = np.random.default_rng(7).normal(0, 300, n)
                for hz, text in radio.ON_AIR.items():
                    pitch = hz - radio.freq
                    if not 300 <= pitch <= 2900:
                        continue
                    if text is None:
                        x += 3000 * np.sin(2 * np.pi * pitch * np.arange(n) / survey.AUDIO_RATE)
                    else:
                        s = psk.send(text * 2, survey.AUDIO_RATE, pitch)[:n]
                        x[:len(s)] += 3000 * s / np.abs(s).max()
                return x.astype(np.float32)

            def close(self) -> None:
                radio.closed = True

        return Sound()


def test_psk31_is_found_by_listening_through_its_part_of_the_band(tmp_path: Path) -> None:
    radio = _PskBand(receiving={"freq": 18_100_000, "mode": "usb"})
    job = _job(radio)
    job.keep_in(tmp_path / "survey.json")
    job.start(RX, survey.clean_places([{"key": "psk31:17 m", "mode": "psk31", "band": "17 m", "hz": 18_096_000, "lo": 18_095_000, "hi": 18_101_000}]))
    job._thread.join(60)
    assert job.status()["done"] == 1 and "error" not in job.status()
    f = job.found(RX)["psk31:17 m"]
    assert f["n"] == 1 and abs(f["where"][0]["hz"] - 18_097_400) < 20 and f["where"][0]["psk"] == 31 and "g4abc" in f["where"][0]["text"]
    dials = [hz for hz, _ in radio.tuned[:-1]]
    assert dials[0] == 18_094_600 and all(b - a == 2_200 for a, b in zip(dials, dials[1:])) and dials[-1] + 400 < 18_101_000     # a couple of kHz at a time, all of it
    assert radio.tuned[-1] == (18_100_000, "usb")    # back where it was
    assert survey.clean_places([{"key": "psk31:20 m", "mode": "psk31", "band": "20 m", "hz": 14_070_000}])[0].get("lo") is None     # (no stretch: the old count)

def test_a_receiver_with_no_waterfall_is_swept_for_nothing_and_the_round_goes_on() -> None:
    radio = _Radio()
    job = _job(radio)
    job._get = lambda url: {"running": False, "rows": []} if "/spectrum" in url else radio.get(url)
    job.start(RX, survey.clean_places([{"key": "cw:40 m", "mode": "cw", "band": "40 m", "hz": 7_020_000, "lo": 7_000_000, "hi": 7_040_000}, PLACES[0]]))
    job._thread.join(30)
    assert job.status()["done"] == 1 and list(job.found(RX)) == ["ft8:40 m"]


def test_a_round_begun_by_itself_leaves_a_receiver_somebody_is_listening_to() -> None:
    radio = _Radio(receiving={"freq": 7_100_000, "mode": "lsb"})
    job = _job(radio)
    job._get = lambda url: {**radio.get(url), "listeners": 1}             # another radio in the house, say
    job.start(RX, survey.clean_places(PLACES[:1]), "itself")
    job._thread.join(30)
    assert "listening" in job.status()["error"] and radio.tuned == [] and job.found(RX) == {}
    job.start(RX, survey.clean_places(PLACES[:1]))                         # asked for: it's the owner's to take
    job._thread.join(30)
    assert job.status()["done"] == 1 and radio.tuned[0] == (7_074_000, "usb")
    busy = calibrate.Job(get=lambda url: {"upconverter": 125, "ppm": 60, "trim": 0, "listeners": 2}, post=lambda url, body: {})
    busy.start(RX, None, True)                                             # ...and the same for a calibration nobody asked for
    busy._thread.join(30)
    assert "listening" in busy.status()["error"]


def test_a_survey_and_a_calibration_never_run_together(monkeypatch) -> None:
    class Busy:
        def status(self) -> dict:
            return {"running": True, "base": RX}
    radio = _Radio()
    monkeypatch.setattr(calibrate, "job", Busy())
    with pytest.raises(ValueError, match="calibrated"):
        _job(radio).start(RX, survey.clean_places(PLACES))
    monkeypatch.undo()
    monkeypatch.setattr(survey, "job", Busy())
    with pytest.raises(ValueError, match="survey"):
        calibrate.Job().start(RX)
    r = ft8.Reader()                                 # ...and nothing is reported from a receiver being taken round
    r.own, r.dial, r.playing, r.report = RX, 14_074_000, (lambda: RX + "/stream"), (lambda *a: None)
    assert r.reports() is False
    monkeypatch.undo()
    assert r.reports() is True


def test_the_places_and_the_plan_are_checked() -> None:
    assert survey.clean_places(PLACES)[0] == PLACES[0]
    for bad in ([], "ft8", [{"key": "a", "mode": "rtty", "band": "x", "hz": 7e6}], [{"key": "a", "mode": "ft8", "band": "x", "hz": "7074000"}],
                [{"key": "", "mode": "ft8", "band": "x", "hz": 7e6}], [PLACES[0], PLACES[0]], [{"mode": "ft8", "hz": 7e6}], PLACES * 20):
        with pytest.raises(ValueError):
            survey.clean_places(bad)
    plan = survey.clean_plan({RX + "/": {"every_h": 3, "places": PLACES[:2]}})
    assert plan == {RX: {"every_h": 3, "places": PLACES[:2]}}
    for bad in ([], {RX: {"every_h": 4, "places": PLACES}}, {RX: {"every_h": True, "places": PLACES}}, {RX: {"every_h": 1, "places": []}}, {"receiver": {"every_h": 1, "places": PLACES}}):
        with pytest.raises(ValueError):
            survey.clean_plan(bad)


def test_by_itself_on_the_hour_when_nobody_is_listening() -> None:
    class Job:
        def __init__(self) -> None:
            self.started, self.running = [], False

        def status(self) -> dict:
            return {"running": self.running}

        def start(self, base, places, why="asked") -> None:
            self.started.append((base, len(places), why))

    job, on, at = Job(), [None], [14.2 * 3600.0]
    rounds = survey.Rounds(job, playing=lambda: on[0], clock=lambda: at[0], localtime=time.gmtime)
    rounds.set_plan(survey.clean_plan({RX: {"every_h": 3, "places": PLACES[:2]}}))
    assert rounds.status() == {"plan": {RX: {"every_h": 3, "places": 2}}}
    rounds.tick()
    assert job.started == []                         # two o'clock: not one of its hours (every three: 0, 3, 6...)
    at[0] = 15.01 * 3600
    on[0] = RX + "/stream"                           # three o'clock, but somebody is listening to it: not now
    rounds.tick()
    assert job.started == []
    on[0] = None
    at[0] += 600
    rounds.tick()
    rounds.tick()
    assert job.started == [(RX, 2, "itself")]        # once in the hour
    at[0] = 18.0 * 3600 + 5
    job.running = True                               # six o'clock, a round begun by hand is still going: wait for it
    rounds.tick()
    job.running = False
    rounds.tick()
    assert len(job.started) == 2
    rounds.set_plan({})                              # taken off the plan: no more
    at[0] = 21.0 * 3600 + 5
    rounds.tick()
    assert len(job.started) == 2


def test_the_survey_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    radio = _Radio()
    monkeypatch.setattr(survey, "job", _job(radio))
    monkeypatch.setattr(survey, "rounds", survey.Rounds(survey.job))

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/rx/survey") == (200, {"running": False, "found": {}, "itself": {"plan": {}}})
        for bad in ({}, {"base": RX}, {"base": "receiver", "places": PLACES}, {"base": RX, "places": [{"key": "a", "mode": "cw", "band": "x", "hz": 7e6}]}):
            assert call("/api/rx/survey", bad)[0] == 400
        code, st = call("/api/rx/survey", {"base": RX + "/", "places": PLACES[:1]})
        assert code == 200 and st["base"] == RX and st["of"] == 1
        survey.job._thread.join(30)
        st = call("/api/rx/survey?base=" + RX)[1]
        assert not st["running"] and st["done"] == 1 and st["found"]["ft8:40 m"]["n"] == 3 and len(st["found"]["ft8:40 m"]["day"]) == 1
        assert call("/api/rx/survey", {"stop": True})[0] == 200
        # by itself: how often, and round which places; kept in the settings
        assert call("/api/rx/survey", {"base": RX, "itself": 5, "places": PLACES[:2]})[0] == 400
        code, st = call("/api/rx/survey", {"base": RX, "itself": 3, "places": PLACES[:2]})
        assert code == 200 and st["itself"] == {"plan": {RX: {"every_h": 3, "places": 2}}} and st["found"]["ft8:40 m"]["n"] == 3
        assert json.loads(conf.read_text())["survey_itself"] == {RX: {"every_h": 3, "places": PLACES[:2]}}
        assert call("/api/rx/survey", {"base": RX, "itself": 0})[1]["itself"] == {"plan": {}} and json.loads(conf.read_text())["survey_itself"] == {}
        assert call("/api/rx/survey", {"base": RX, "forget": True})[1]["found"] == {}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_bands_week_one_finding_an_hour(tmp_path: Path) -> None:
    radio = _Radio()
    job = _job(radio)
    file = tmp_path / "survey.json"
    job.keep_in(file)
    place = survey.clean_places(PLACES[:1])
    hour0 = (T0 // 3600 + 1) * 3600                   # (the top of an hour)
    for n, at in ((3, hour0 + 60), (5, hour0 + 1800), (2, hour0 + 3000), (4, hour0 + 3 * 86400), (1, hour0 + 8 * 86400)):
        radio.HEARD = {7_074_000: [{"snr": 0.0, "dt": 0.5, "hz": 800 + i, "text": f"CQ G{i}ABC IO91"} for i in range(n)]}
        radio.at = at
        job.start(RX, place, "itself")
        job._thread.join(30)
    assert "week" not in job.found(RX)["ft8:40 m"]                       # (not sent to the page with every look)
    week = job.found(RX, week=True)["ft8:40 m"]["week"]
    assert [n for _, n in week] == [4, 1]                                # the first hour's three rounds (most: 5) are over a week old now
    kept = json.loads(file.read_text())["found"][RX]["ft8:40 m"]["week"]
    again = survey.Job(clock=radio.clock)
    again.keep_in(file)
    assert again.found(RX, week=True)["ft8:40 m"]["week"] == [[float(t), n] for t, n in kept]
