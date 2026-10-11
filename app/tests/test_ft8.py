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


def _reader(found=None, at: float = T0 - 1.0):
    """A Reader on a clock of ours, with a decoder that keeps what it was given. -> (reader, clock, given)"""
    clock, given = Clock(at), []

    def decode(samples: np.ndarray, mode: str) -> list[dict]:
        given.append((samples, mode))
        return [dict(m) for m in (found or [])]

    r = ft8.Reader(clock=clock, decode=decode)
    r.on = True                                      # (no thread: stepped here)
    r.step()
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
    assert abs(loud[0] - (3.0 - ft8.CUT_LATE_S)) < 0.15 and abs(loud[-1] - (5.0 - ft8.CUT_LATE_S)) < 0.15
    assert np.abs(second).max() < 100                # (the next slot: none of it)
    st = r.status()
    assert st["slots"] == 2 and st["last"] == {"t": T0 + slot, "heard": 1} and st["slot"] == slot and st["mode"] == mode
    assert [(m["n"], m["t"], m["text"]) for m in st["messages"]] == [(1, T0, "CQ K1ABC FN42"), (2, T0 + slot, "CQ K1ABC FN42")]
    assert [m["n"] for m in r.status(since=1)["messages"]] == [2]


def test_sound_that_comes_late_is_cut_the_same() -> None:
    r, clock, given = _reader()
    x = np.zeros(int((1.0 + 15.0 + 5.0) * pcm.SAMPLE_RATE))
    a = int((1.0 + 3.0) * pcm.SAMPLE_RATE)
    x[a:a + 2 * pcm.SAMPLE_RATE] = _tone(2.0, 1000.0)
    _play(r, clock, x, late=2.0)                     # (a receiver across the internet)
    loud = np.where(np.abs(given[0][0].astype(float)) > 2000)[0] / ft8.OUT_RATE
    assert abs(loud[0] - (3.0 + 2.0 - ft8.CUT_LATE_S)) < 0.15       # later in the slot by as much: the decoder allows for it


def test_a_slot_with_nothing_playing_is_left() -> None:
    r, clock, given = _reader()
    _play(r, clock, np.zeros(int(6.0 * pcm.SAMPLE_RATE)))            # the receiver stops 5 s into the slot...
    clock.at = T0 + 15.0 + ft8.CUT_LATE_S + ft8.WAIT_S + 0.1
    r.step()
    assert given == [] and r.status()["slots"] == 0
    clock.at = T0 + 200.0                            # ...and starts again much later: from the slot then, not the ones gone
    r.step()
    _play(r, clock, np.zeros(int(32.0 * pcm.SAMPLE_RATE)))
    assert len(given) == 1 and r.status()["last"]["t"] == T0 + 210.0


def test_a_reader_thats_off_keeps_nothing() -> None:
    r, clock, given = _reader()
    r.on = False
    r.feed(_speaker(_tone(1.0, 1000.0)))
    assert len(r._x) == 0
    r.on = True
    for _ in range(600):                             # a minute fed and never read: only so much is kept
        clock.at += BLOCK_S
        r.feed(_speaker(_tone(BLOCK_S, 1000.0)))
    assert len(r._x) == int(ft8.KEEP_S * ft8.RATE)
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
    for late in (0.0, 2.0):
        clock = Clock(T0 - 1.0)
        r = ft8.Reader(clock=clock)
        r.on = True
        r.step()
        _play(r, clock, np.concatenate((np.zeros(pcm.SAMPLE_RATE), played, np.zeros(3 * pcm.SAMPLE_RATE))), late=late)
        got = r.status()["messages"]
        assert [m["text"] for m in got] == ["CQ K1ABC FN42"], late
        assert abs(got[0]["hz"] - 1200) <= 4 and got[0]["t"] == T0


def test_the_reader_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("")
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
        monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(tmp_path / "not-there"))
        assert call("/api/rx/ft8")[1]["can"] is False
        assert call("/api/rx/ft8", {"on": True})[0] == 400 and not ft8.reader.on       # no decoder: it says so
        monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(decoder))
        st = call("/api/rx/ft8")[1]
        assert st["on"] is False and st["can"] is True and st["messages"] == []
        assert call("/api/rx/ft8", {"on": True}) == (200, {"ft8": "ft8"}) and ft8.reader.on
        assert call("/api/rx/ft8", {"on": True, "mode": "ft4"}) == (200, {"ft8": "ft4"}) and ft8.reader.mode == "ft4"
        for bad in ({"on": "yes"}, {"on": True, "mode": "rtty"}, {}):
            assert call("/api/rx/ft8", bad)[0] == 400 and ft8.reader.mode == "ft4"
        code, st = call("/api/rx/ft8?since=3")
        assert code == 200 and st["on"] is True and st["mode"] == "ft4" and st["slot"] == 7.5
        assert call("/api/rx/ft8", {"on": False}) == (200, {"ft8": None}) and not ft8.reader.on
    finally:
        ft8.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()
