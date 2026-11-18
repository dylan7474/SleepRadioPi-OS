import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np

from sleepradiopi.audio import spectrum
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.web.server import make_handler

RATE = 44_100


def _sine(hz, amp, n=spectrum.SCOPE_N):
    return amp * np.sin(2 * np.pi * hz * np.arange(n) / RATE)


def test_a_tone_lands_in_its_band_at_its_level() -> None:
    db = spectrum.bands(_sine(1000, 1.0), RATE)
    i = spectrum.CENTRES.index(1000)
    assert abs(db[i]) < 0.5                                    # a full-scale sine: 0 dB
    assert all(v < db[i] - 30 for k, v in enumerate(db) if abs(k - i) > 1)
    half = spectrum.bands(_sine(1000, 0.5), RATE)
    assert abs(half[i] - (-6.0)) < 0.5
    low = spectrum.bands(_sine(40, 0.5), RATE)                  # the lowest bands resolve too
    assert int(np.argmax(low)) == spectrum.CENTRES.index(40)
    assert spectrum.bands(np.zeros(spectrum.SCOPE_N), RATE) == [spectrum.FLOOR_DB] * len(spectrum.CENTRES)
    assert len(spectrum.CENTRES) == 28


def test_the_speaker_keeps_samples_only_while_someone_looks(tmp_path) -> None:
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    spk.volume = 50
    spk.start()
    tone = (np.repeat(_sine(2000, 0.25, 4 * spectrum.SCOPE_N)[:, None], 2, axis=1) * 32767).astype(np.int16)
    spk.write(tone)
    assert len(spk._scope) == 0                                 # nobody looking: nothing kept
    first = spk.spectrum()                                      # a look starts it...
    assert not first["live"]
    spk.write(tone)
    d = spk.spectrum()                                          # ...and the next audio is there
    assert d["live"] and d["hz"][int(np.argmax(d["db"]))] == 2000
    assert -14 < max(d["db"]) < -10                             # a quarter of full scale: -12 dB, whatever the volume
    spk.stop()


def test_web_api_gives_the_bands() -> None:
    class FakeStation:
        def status(self):
            return {"on_air": True}
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/spectrum") as r:
            d = json.load(r)
        assert d["hz"][0] == 31.5 and len(d["db"]) == 28 and d["live"] is False
    finally:
        httpd.shutdown()


def test_the_steady_sound_goes_through_the_eq_and_other_tests_dont(tmp_path) -> None:
    from sleepradiopi.audio.eq import Equalizer
    from sleepradiopi.audio.testsignal import TestSignal
    out = tmp_path / "played.raw"
    spk = SpeakerOutput(command=["sh", "-c", f"cat >> {out}"], eq=Equalizer(RATE, 2, {"g1k": -12}))
    spk.volume = 100
    spk.start()
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    ctl.play()
    ctl.start_test("steady")
    show = np.zeros((spectrum.SCOPE_N, 2), dtype=np.int16)
    spk.spectrum()
    for _ in range(6):
        spk.write(show)
    cut = spk.spectrum()["db"]
    i, j = spectrum.CENTRES.index(1000), spectrum.CENTRES.index(4000)
    ref = spectrum.bands(TestSignal("steady", RATE, 1).next(spectrum.SCOPE_N)[:, 0] / 32768, RATE)
    assert (ref[i] - ref[j]) - (cut[i] - cut[j]) > 6                # the 1 kHz cut is in it
    ctl.stop_test()
    spk.stop()


def test_hearing_check_tones() -> None:
    from sleepradiopi.audio.testsignal import TestSignal
    t = TestSignal("tone", RATE, 2, hz=4000, db=-20)
    x = t.next(t.total)[:, 0]
    assert t.total == int(3 * (0.25 + 0.2) * RATE) and "4000 Hz" in t.label
    assert abs(20 * np.log10(np.max(np.abs(x)) / 32767) - (-20)) < 0.2         # its level
    beep = x[: int(0.25 * RATE)] / 32767
    db = spectrum.bands(np.concatenate([beep] * 40)[: spectrum.SCOPE_N], RATE)
    assert spectrum.CENTRES[int(np.argmax(db))] == 4000                          # its pitch
    assert np.all(x[int(0.26 * RATE): int(0.44 * RATE)] == 0)                    # a gap between beeps
    quiet = TestSignal("tone", RATE, 2, hz=1000, db=-200)                         # floored, never silent by mistake
    assert np.max(np.abs(quiet.next(quiet.total))) > 0


def test_web_api_tone_checks_its_numbers() -> None:
    import urllib.error
    import pytest

    class FakeStation:
        def status(self):
            return {"on_air": True}
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/speaker", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    try:
        assert post({"test": "tone", "hz": 8000, "db": -40})["test"]["kind"] == "tone"
        for bad in ({"test": "tone", "hz": 5}, {"test": "tone", "db": 3}, {"test": "tone", "hz": True}):
            with pytest.raises(urllib.error.HTTPError) as e:
                post(bad)
            assert e.value.code == 400
    finally:
        httpd.shutdown()


def test_power_dips_are_counted_while_watched(tmp_path, monkeypatch) -> None:
    """A dip counts once however long it lasts. (The alarm is fed as a fixed run of readings, not flipped against
    the clock: with real sleeps a busy machine sampled a change late or twice, and the test failed now and then.)"""
    import time
    d = tmp_path / "hwmon1"
    d.mkdir()
    (d / "name").write_text("rpi_volt\n")
    alarm = d / "in0_lcrit_alarm"
    alarm.write_text("0\n")
    w = spectrum.PowerWatch(tmp_path)
    assert w._read() is False
    alarm.write_text("1\n")
    assert w._read() is True                                      # (the real sensor file is read)
    readings = [False, True, True, False, True, False]            # (the first is where it starts)

    def read():
        v = readings.pop(0)
        if not readings:
            w._until = 0.0                                         # (the run of readings is over: stop)
        return v
    w._read = read
    monkeypatch.setattr(spectrum.time, "sleep", lambda s: None)
    w._until = float("inf")
    w._run()
    assert w.dips == 2                                             # two dips, however long each lasted
    quiet = spectrum.PowerWatch(tmp_path)
    alarm.write_text("0\n")
    assert quiet.watch(time.monotonic() + 0.05) == 0              # (watching starts, nothing yet)
    assert spectrum.PowerWatch(tmp_path / "none").watch(time.monotonic() + 1) is None   # no sensor: None
