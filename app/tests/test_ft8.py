"""The FT8 / FT4 reader for receivers (audio/ft8.py)."""
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import ft8, pcm
from sleepradiopi.web.server import make_handler

T0 = 1_800_000_000.0                                 # a moment that begins a slot, FT8's and FT4's
BLOCK_S = 0.1
LATE = 1.0                                           # how late the sound is taken to be, where a test says it's known


class Clock:
    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _tone(seconds: float, pitch: float, level: float = 8000.0, rate: float = pcm.SAMPLE_RATE) -> np.ndarray:
    return level * np.sin(2 * np.pi * pitch * np.arange(int(seconds * rate)) / rate)


def _speaker(x: np.ndarray) -> np.ndarray:
    """Sound at the speaker's rate -> as the station plays it (both channels)."""
    return np.repeat(np.clip(x, -32768, 32767).astype(np.int16)[:, None], pcm.CHANNELS, axis=1)


def _play(r: ft8.Reader, clock: Clock, x: np.ndarray, late: float = 0.0) -> None:
    """Play x (at the speaker's rate) to r a block at a time, each arriving `late` after it was sent, stepping it as its thread would."""
    n = int(BLOCK_S * pcm.SAMPLE_RATE)
    sent = clock.at
    for i in range(0, len(x), n):
        sent += BLOCK_S
        clock.at = sent + late
        r.feed(_speaker(x[i:i + n]))
        r.step()


def _reader(found=None, at: float = T0 - 1.0, late: float | None = LATE):
    """A Reader on a clock of ours, with a decoder that keeps what it was given, and the sound's delay known (None:
    to be found). -> (reader, clock, given)"""
    clock, given = Clock(at), []

    def decode(samples: np.ndarray, mode: str) -> list[dict]:
        given.append((samples, mode))
        return [dict(m) for m in (found or [])]

    r = ft8.Reader(clock=clock, decode=decode)
    r.on = True                                      # (no thread: stepped here)
    r.step()
    r._late = late
    return r, clock, given


def test_the_decoders_rate_keeps_a_tone_as_it_was() -> None:
    for pitch in (300.0, 1500.0, 2900.0):
        y = ft8.to_decoder_rate(_tone(2.0, pitch, rate=ft8.RATE).astype(np.float32)).astype(float)
        assert len(y) == 2 * ft8.OUT_RATE
        mid = y[2000:-2000]
        spectrum = np.abs(np.fft.rfft(mid * np.hanning(len(mid))))
        assert abs(np.fft.rfftfreq(len(mid), 1 / ft8.OUT_RATE)[spectrum.argmax()] - pitch) < 1.0
        assert 0.85 < np.sqrt(2 * np.mean(mid ** 2)) / 8000.0 < 1.05     # ...and about as loud
    assert len(ft8.to_decoder_rate(np.zeros(0, np.float32))) == 0


def test_what_the_decoder_prints_is_read() -> None:
    out = ("Sample rate 12000 Hz, 180000 samples, 15.000 seconds\nDecoded 2 messages, callsign hashtable size 4\n"
           "000000 -12.5 +0.48 1203 ~  K1ABC W9XYZ -11\n000000 +17.0 +0.96  222 ~  CQ OE3UKW JN88\nMax magnitude: -18.3 dB\n")
    assert ft8.parse(out) == [{"snr": 17.0, "dt": 0.96, "hz": 222, "text": "CQ OE3UKW JN88"},      # strongest first
                              {"snr": -12.5, "dt": 0.48, "hz": 1203, "text": "K1ABC W9XYZ -11"}]
    assert ft8.parse("") == [] and ft8.parse("Usage: decode_ft8 ...\n") == []


@pytest.mark.parametrize("mode", ["ft8", "ft4"])
def test_a_slot_is_cut_where_the_clock_says(mode: str) -> None:
    slot = ft8.MODES[mode]
    r, clock, given = _reader([{"snr": 3.0, "dt": 0.5, "hz": 1000, "text": "CQ K1ABC FN42"}])
    r.mode = mode
    # a second of nothing, then two slots and a bit; a tone for the two seconds from 3 s into the first slot
    x = np.zeros(int((1.0 + 2 * slot + 3.0) * pcm.SAMPLE_RATE))
    a = int((1.0 + 3.0) * pcm.SAMPLE_RATE)
    x[a:a + 2 * pcm.SAMPLE_RATE] = _tone(2.0, 1000.0)
    _play(r, clock, x)
    assert [m for _, m in given] == [mode, mode]
    first, second = given[0][0].astype(float), given[1][0].astype(float)
    assert len(first) == len(second) == int(slot * ft8.OUT_RATE)
    loud = np.where(np.abs(first) > 2000)[0] / ft8.OUT_RATE
    assert abs(loud[0] - (3.0 - LATE)) < 0.15 and abs(loud[-1] - (5.0 - LATE)) < 0.15
    assert np.abs(second).max() < 100                # (the next slot: none of it)
    st = r.status()
    assert st["slots"] == 2 and st["last"] == {"t": T0 + slot, "heard": 1} and st["slot"] == slot and st["mode"] == mode
    assert [(m["n"], m["t"], m["text"]) for m in st["messages"]] == [(1, T0, "CQ K1ABC FN42"), (2, T0 + slot, "CQ K1ABC FN42")]
    assert [m["n"] for m in r.status(since=1)["messages"]] == [2]


@pytest.mark.parametrize("late", [0.4, 3.6, 6.0])
def test_how_late_the_sound_is_is_found_and_followed(late: float) -> None:
    """A receiver the radio tunes itself is half a second behind; one played through its buffer was 3.6 s behind on
    the radio, and with the cut a second on nothing of a full band was read."""
    clock, given = Clock(T0 - 1.0), []

    def decode(samples: np.ndarray, mode: str) -> list[dict]:
        # as the decoder is: it reads a message that begins from a second and a half before to two seconds after
        # where it should (half a second in), and says where it began. The "message" is a tone from 3 s into each slot.
        given.append(samples)
        loud = np.where(np.abs(samples.astype(float)) > 2000)[0]
        dt = (loud[0] / ft8.OUT_RATE - 3.0 + ft8.ON_TIME_DT) if len(loud) else None
        return [{"snr": 3.0, "dt": round(dt, 2), "hz": 1000, "text": "CQ K1ABC FN42"}] if dt is not None and -1.0 <= dt <= 2.5 else []

    r = ft8.Reader(clock=clock, decode=decode)
    r.on = True
    r.step()
    x = np.zeros(int((1.0 + 4 * 15.0 + 9.0) * pcm.SAMPLE_RATE))
    for k in range(4):
        a = int((1.0 + 15.0 * k + 3.0) * pcm.SAMPLE_RATE)
        x[a:a + 2 * pcm.SAMPLE_RATE] = _tone(2.0, 1000.0)
    _play(r, clock, x, late=late)
    st = r.status()
    assert [m["t"] for m in st["messages"]] == [T0, T0 + 15.0, T0 + 30.0, T0 + 45.0]      # every slot read, each once
    assert abs(st["late"] - late) < 0.2 and all(abs(m["dt"] - ft8.ON_TIME_DT) < 0.2 for m in st["messages"][1:])
    assert 1 + 3 <= len(given) <= len(ft8.SEARCH_LATE_S) + 3     # the first slot cut each way there was the sound for, then one cut a slot


def test_a_band_gone_quiet_then_another_delay() -> None:
    r, clock, given = _reader()
    r._decode = lambda samples, mode: given.append(samples) or []
    _play(r, clock, np.zeros(int((1.0 + ft8.LOST_AFTER * 15.0 + 3.0) * pcm.SAMPLE_RATE)))
    assert len(given) == ft8.LOST_AFTER and r.status()["late"] is None       # nothing in that many slots: every cut tried again
    _play(r, clock, np.zeros(int(23.0 * pcm.SAMPLE_RATE)))
    assert len(given) == ft8.LOST_AFTER + len(ft8.SEARCH_LATE_S)


def test_a_slot_with_nothing_playing_is_left() -> None:
    r, clock, given = _reader()
    _play(r, clock, np.zeros(int(6.0 * pcm.SAMPLE_RATE)))            # the receiver stops 5 s into the slot...
    clock.at = T0 + 15.0 + LATE + ft8.WAIT_S + 0.1
    r.step()
    assert given == [] and r.status()["slots"] == 0
    clock.at = T0 + 200.0                            # ...and starts again much later: from the slot then, not the ones gone
    r.step()
    _play(r, clock, np.zeros(int(34.0 * pcm.SAMPLE_RATE)))
    assert r.status()["last"]["t"] == T0 + 210.0 and r.status()["slots"] == 2      # (what there was most of the sound for, and the next)
    assert len(given) > 2                            # (a new start: its delay looked for, each slot cut more than one way)


def test_a_reader_thats_off_keeps_nothing() -> None:
    r, clock, given = _reader()
    r.on = False
    r.feed(_speaker(_tone(1.0, 1000.0)))
    assert len(r._x) == 0
    r.on = True
    for _ in range(600):                             # a minute fed and never read: only so much is kept
        clock.at += BLOCK_S
        r.feed(_speaker(_tone(BLOCK_S, 1000.0)))
    keep = (ft8.KEEP_SLOTS * 15.0 + ft8.KEEP_MORE_S) * ft8.RATE
    assert keep <= len(r._x) < keep + BLOCK_S * ft8.RATE + 1
    clock.at += 30.0                                 # the receiver stops, and starts again: what was kept is from another time
    r.feed(_speaker(_tone(BLOCK_S, 1000.0)))
    assert len(r._x) == int(BLOCK_S * ft8.RATE)


def test_a_decoder_that_fails_is_said() -> None:
    clock = Clock(T0 - 1.0)

    def decode(samples, mode):
        raise RuntimeError("the decoder took too long")

    r = ft8.Reader(clock=clock, decode=decode)
    r.on = True
    r.step()
    r._late = LATE
    _play(r, clock, np.zeros(int(18.0 * pcm.SAMPLE_RATE)))
    st = r.status()
    assert st["last"] == {"t": T0, "error": "the decoder took too long"} and st["slots"] == 0 and st["messages"] == []


def test_the_switch(monkeypatch) -> None:
    r = ft8.Live(decode=lambda samples, mode: [])
    assert not r.on and r.mode == "ft8"
    try:
        r.switch(True, "ft4")
        assert r.on and r.mode == "ft4" and r.status()["slot"] == 7.5
        r._messages.append({"n": 1, "t": T0, "snr": 0.0, "dt": 0.0, "hz": 1000, "text": "CQ K1ABC FN42"})
        r.switch(True, "ft4")                        # as it is: nothing lost
        assert len(r.status()["messages"]) == 1
        r.switch(True, "ft8")                        # the other mode: from nothing
        assert r.mode == "ft8" and r.status()["messages"] == []
        with pytest.raises(ValueError):
            r.switch(True, "rtty")
        assert r.on and r.mode == "ft8"
    finally:
        r.switch(False)
    assert not r.on and r.mode == "ft8"


def test_locators_and_how_far() -> None:
    assert ft8.clean_locator(" io94 ") == "IO94" and ft8.clean_locator("io94KN") == "IO94kn" and ft8.clean_locator("") == "" and ft8.clean_locator(None) == ""
    for bad in ("IO9", "SS94", "IO94k", "IO94zz", "London"):
        with pytest.raises(ValueError):
            ft8.clean_locator(bad)
    assert ft8.locate("IO94") == (54.5, -1.0) and ft8.locate("JJ00") == (0.5, 1.0)
    lat, lon = ft8.locate("IO94kn")
    assert abs(lat - 54.5625) < 1e-6 and abs(lon + 1.125) < 1e-6
    km, deg = ft8.path("IO94", "FN42")               # the north-east of England to Boston: about 5100 km, a little north of west
    assert 5000 < km < 5200 and 280 < deg < 295
    km, deg = ft8.path("IO94", "PM95")               # ...to Tokyo: over the top
    assert 9200 < km < 9500 and 25 < deg < 40
    assert ft8.path("IO94", "IO94") == (0, 0)
    assert ft8.path("IO94", "IO84")[1] == 270 or abs(ft8.path("IO94", "IO84")[1] - 270) <= 2      # the next square west


def test_who_sent_it() -> None:
    assert ft8.who("CQ K1ABC FN42") == ("K1ABC", "FN42", True)
    assert ft8.who("CQ DX K1ABC FN42") == ("K1ABC", "FN42", True) and ft8.who("CQ POTA EA2WX/P IN83") == ("EA2WX/P", "IN83", True)
    assert ft8.who("CQ IQ9SZ/7") == ("IQ9SZ/7", None, True)
    assert ft8.who("K1ABC W9XYZ EN37") == ("W9XYZ", "EN37", False)
    assert ft8.who("K1ABC W9XYZ R-11") == ("W9XYZ", None, False) and ft8.who("K1ABC W9XYZ RR73") == ("W9XYZ", None, False)
    assert ft8.who("<...> DA125RH 73") == ("DA125RH", None, False) and ft8.who("K1ABC <...> -05") == (None, None, False)
    assert ft8.who("<K1ABC> <W9XYZ> R-05") == ("W9XYZ", None, False)
    assert ft8.who("TNX BOB 73 GL") == (None, None, False) and ft8.who("") == (None, None, False)


def test_who_has_been_heard(monkeypatch) -> None:
    said = [[{"snr": 3.0, "dt": 0.5, "hz": 1000, "text": "CQ K1ABC FN42"}, {"snr": -5.0, "dt": 0.4, "hz": 1500, "text": "G4XYZ JA1VSL PM95"}],
            [{"snr": 7.0, "dt": 0.5, "hz": 1000, "text": "G4XYZ K1ABC -12"}, {"snr": 1.0, "dt": 0.5, "hz": 700, "text": "TNX BOB 73 GL"}]]
    clock = Clock(T0 - 1.0)
    r = ft8.Reader(clock=clock, decode=lambda samples, mode: [dict(m) for m in said.pop(0)])
    r.on, r.band = True, "20 m"
    r.step()
    r._late = LATE
    monkeypatch.setattr(ft8, "HOME", None)
    _play(r, clock, np.zeros(int(34.0 * pcm.SAMPLE_RATE)))
    got = r.status()["messages"]
    assert [(m.get("call"), m.get("grid"), "km" in m) for m in got] == [("K1ABC", "FN42", False), ("JA1VSL", "PM95", False), ("K1ABC", "FN42", False), (None, None, False)]
    monkeypatch.setattr(ft8, "HOME", "IO94")         # the radio's locator known: how far and which way (a later message: by the locator given before)
    got, st = r.status()["messages"], r.status()
    assert st["home"] == "IO94" and st["band"] == "20 m"
    assert 5000 < got[2]["km"] < 5200 and 280 < got[2]["deg"] < 295 and "km" not in got[3]
    h = r.heard()
    assert h["band"] == "20 m" and [x["call"] for x in h["heard"]] == ["K1ABC", "JA1VSL"]          # the latest heard first
    k = h["heard"][0]
    assert (k["band"], k["grid"], k["snr"], k["first"], k["last"], k["count"], k["cq"]) == ("20 m", "FN42", 7.0, T0, T0 + 15.0, 2, False)
    assert 5000 < k["km"] < 5200 and 9200 < h["heard"][1]["km"] < 9500
    r.band = "40 m"                                  # another band: the same station again is another line
    r._decode = lambda samples, mode: [{"snr": 0.0, "dt": 0.5, "hz": 900, "text": "CQ K1ABC FN42"}]
    _play(r, clock, np.zeros(int(15.0 * pcm.SAMPLE_RATE)))
    assert [(x["band"], x["call"], x["count"]) for x in r.heard()["heard"]] == [("40 m", "K1ABC", 1), ("20 m", "K1ABC", 2), ("20 m", "JA1VSL", 1)]
    r.forget()
    assert r.heard()["heard"] == []


def test_what_wsprd_prints_is_read() -> None:
    out = ("slot -19 -0.2   0.001523  3  G4HSB IO94 23 \nslot -24 -0.1   0.001555  3  OK1BF JO70 23 \n"
           "slot -25 -1.5   0.001600  0  <...> KM56VO 37 \nslot -21  0.4   0.001440 -1  PJ4/K1ABC 30 \nslot -20  0.4   0.001470  0  <G0ABC> IO91wm 27 \n<DecodeFinished>\n")
    spots = ft8.parse_wspr(out)
    assert [s["text"] for s in spots] == ["G4HSB IO94 23", "<G0ABC> IO91wm 27", "PJ4/K1ABC 30", "OK1BF JO70 23", "<...> KM56VO 37"]      # strongest first
    by = {s["text"].split()[0]: s for s in spots}
    assert by["G4HSB"] == {"snr": -19.0, "dt": -0.2, "hz": 1523, "drift": 3, "text": "G4HSB IO94 23", "call": "G4HSB", "grid": "IO94", "dbm": 23}
    assert by["<G0ABC>"]["call"] == "G0ABC" and by["<G0ABC>"]["grid"] == "IO91wm"                 # (the longer locator, with its call known again)
    assert "grid" not in by["PJ4/K1ABC"] and by["PJ4/K1ABC"]["dbm"] == 30 and "call" not in by["<...>"] and by["<...>"]["dbm"] == 37
    assert ft8.parse_wspr("") == [] and ft8.parse_wspr("<DecodeFinished>\n") == []


def test_wspr_a_two_minute_slot_at_a_time(monkeypatch) -> None:
    spot = {"snr": -19.0, "dt": -0.2, "hz": 1523, "drift": 3, "text": "G4HSB IO94 23", "call": "G4HSB", "grid": "IO94", "dbm": 23}
    r, clock, given = _reader([spot], at=T0 - 1.0, late=None)
    r.mode = "wspr"
    assert T0 % 120 == 0
    x = np.zeros(int((1.0 + 120.0 + 118.0) * pcm.SAMPLE_RATE), np.float32)
    a = int((1.0 + 10.0) * pcm.SAMPLE_RATE)
    x[a:a + 2 * pcm.SAMPLE_RATE] = _tone(2.0, 1500.0)
    _play(r, clock, x)
    assert [m for _, m in given] == ["wspr", "wspr"] and len(given[0][0]) == int(114.0 * ft8.OUT_RATE)     # cut once each, 114 s of the 120
    loud = np.where(np.abs(given[0][0].astype(float)) > 2000)[0] / ft8.OUT_RATE
    assert abs(loud[0] - (10.0 - ft8.WSPR_LATE_S)) < 0.15                    # two seconds late, whatever is read
    st = r.status()
    assert st["slot"] == 120.0 and st["slots"] == 2 and [(m["t"], m["call"], m["grid"], m["dbm"]) for m in st["messages"]] == [(T0, "G4HSB", "IO94", 23), (T0 + 120.0, "G4HSB", "IO94", 23)]
    monkeypatch.setattr(ft8, "HOME", "IO94kn")
    h = r.heard()["heard"]
    assert [(x["call"], x["count"], x["grid"]) for x in h] == [("G4HSB", 2, "IO94")] and h[0]["km"] < 40 and "km" in r.status()["messages"][0]
    assert len(r._x) <= (ft8.KEEP_SLOTS * 120.0 + ft8.KEEP_MORE_S) * ft8.RATE + 1500      # (the sound kept is for the mode: four minutes of it)


def test_no_decoder_no_reading(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(tmp_path / "not-there"))
    assert ft8.decoder_path() is None and ft8.Reader().status()["can"] is False
    with pytest.raises(RuntimeError):
        ft8.run_decoder(np.zeros(100, np.int16), "ft8")


def _generator() -> str | None:
    """ft8_lib's gen_ft8, beside its decoder (neither is on a developer's machine unless they built them)."""
    decoder = ft8.decoder_path()
    beside = os.path.join(os.path.dirname(decoder), "gen_ft8") if decoder else None
    return beside if beside and os.access(beside, os.X_OK) else shutil.which("gen_ft8")


@pytest.mark.skipif(ft8.decoder_path() is None or _generator() is None, reason="needs ft8_lib's decode_ft8 and gen_ft8 (SLEEPRADIO_FT8_DECODER)")
def test_a_real_message_through_the_real_decoder(tmp_path: Path) -> None:
    subprocess.run([_generator(), "CQ K1ABC FN42", str(tmp_path / "sent.wav"), "1200"], check=True, capture_output=True)
    with wave.open(str(tmp_path / "sent.wav")) as w:
        assert w.getframerate() == ft8.OUT_RATE
        sent = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
    n = int(len(sent) * pcm.SAMPLE_RATE / ft8.OUT_RATE)                  # as the radio plays a receiver: at the speaker's rate
    played = np.interp(np.arange(n) * ft8.OUT_RATE / pcm.SAMPLE_RATE, np.arange(len(sent)), sent)
    played = played + np.random.default_rng(1).normal(0, 300.0, n)
    for late in (0.0, 2.0, 4.5):                     # (however late it comes, within reason: found)
        clock = Clock(T0 - 1.0)
        r = ft8.Reader(clock=clock)
        r.on = True
        r.step()
        _play(r, clock, np.concatenate((np.zeros(pcm.SAMPLE_RATE), played, np.zeros(9 * pcm.SAMPLE_RATE))), late=late)
        got, st = r.status()["messages"], r.status()
        assert [m["text"] for m in got] == ["CQ K1ABC FN42"], late
        # (the generator puts its message in the middle of the 15 s, 1.2 s in: 0.7 s later than one sent on the air)
        assert abs(got[0]["hz"] - 1200) <= 4 and got[0]["t"] == T0 and abs(st["late"] - late - 0.7) < 0.4, (late, st["late"])


def test_the_reader_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    decoder = tmp_path / "decode_ft8"
    decoder.write_text("#!/bin/sh\n")
    decoder.chmod(0o755)

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        monkeypatch.setattr(ft8, "HOME", None)
        monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(tmp_path / "not-there"))
        assert call("/api/rx/ft8")[1]["can"] is False
        assert call("/api/rx/ft8", {"on": True})[0] == 400 and not ft8.reader.on       # no decoder: it says so
        monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(decoder))
        st = call("/api/rx/ft8")[1]
        assert st["on"] is False and st["can"] is True and st["messages"] == []
        assert call("/api/rx/ft8", {"on": True}) == (200, {"cw": False, "ft8": "ft8", "keyboard": None}) and ft8.reader.on
        assert call("/api/rx/ft8", {"on": True, "mode": "ft4"})[1]["ft8"] == "ft4" and ft8.reader.mode == "ft4"
        for bad in ({"on": "yes"}, {"on": True, "mode": "rtty"}, {}):
            assert call("/api/rx/ft8", bad)[0] == 400 and ft8.reader.mode == "ft4"
        code, st = call("/api/rx/ft8?since=3")
        assert code == 200 and st["on"] is True and st["mode"] == "ft4" and st["slot"] == 7.5
        monkeypatch.setenv("SLEEPRADIO_WSPR_DECODER", str(tmp_path / "not-there"))
        assert call("/api/rx/ft8", {"on": True, "mode": "wspr"})[0] == 400 and call("/api/rx/ft8")[1]["can_wspr"] is False      # no wsprd: it says so
        monkeypatch.setenv("SLEEPRADIO_WSPR_DECODER", str(decoder))
        assert call("/api/rx/ft8", {"on": True, "mode": "wspr"})[1]["ft8"] == "wspr" and call("/api/rx/ft8")[1]["slot"] == 120.0
        assert call("/api/rx/ft8", {"on": True, "mode": "ft4"})[1]["ft8"] == "ft4"
        assert call("/api/rx/ft8", {"on": True, "mode": "ft4", "band": "40 m"})[0] == 200 and call("/api/rx/ft8")[1]["band"] == "40 m"      # the band alone: nothing begun again
        assert call("/api/rx/ft8", {"on": True, "band": 40})[0] == 400
        assert call("/api/rx/ft8", {"on": False})[1]["ft8"] is None and not ft8.reader.on
        assert call("/api/rx/ft8/heard")[1] == {"band": "40 m", "home": None, "heard": []}
        assert call("/api/rx/ft8/heard", {"clear": True})[1]["heard"] == []
        assert call("/api/rx/locator", {"locator": "io94kn"}) == (200, {"home": "IO94kn"}) and ft8.HOME == "IO94kn"
        assert json.loads(conf.read_text())["locator"] == "IO94kn" and call("/api/rx/ft8")[1]["home"] == "IO94kn"
        code, err = call("/api/rx/locator", {"locator": "Guisborough"})
        assert code == 400 and "IO94" in err["error"] and ft8.HOME == "IO94kn"
        assert call("/api/rx/locator", {"locator": ""}) == (200, {"home": None}) and ft8.HOME is None
    finally:
        ft8.reader.switch(False)
        ft8.reader.band, ft8.HOME = None, None
        httpd.shutdown()
        httpd.server_close()
