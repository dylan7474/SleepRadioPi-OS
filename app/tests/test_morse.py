"""The Morse reader for receivers (audio/morse.py)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

from sleepradiopi.audio import morse, pcm
from sleepradiopi.web.server import make_handler

RATE = morse.RATE
CODE = {v: k for k, v in morse.MORSE.items()}


def _keyed(text: str, wpm: float, pitch: float, rate: float = RATE, shaky: float = 0.0, seed: int = 1, lead: float = 1.0) -> np.ndarray:
    """A Morse tone (amplitude 1) with a second of nothing either side; shaky: the spread of each length."""
    rng = np.random.default_rng(seed)
    dit = 1.2 / wpm
    j = (lambda units: units * dit * float(np.exp(rng.normal(0, shaky)))) if shaky else (lambda units: units * dit)
    t, spans = lead, []
    for word in text.split(" "):
        for ch in word:
            for e in CODE[ch]:
                d = j(1 if e == "." else 3)
                spans.append((t, t + d))
                t += d + j(1)
            t += j(2)
        t += j(4)
    key = np.zeros(int((t + lead) * rate))
    for a, b in spans:
        key[int(a * rate):int(b * rate)] = 1.0
    ramp = np.hanning(2 * int(rate * 0.005) + 1)                           # 5 ms edges
    return np.convolve(key, ramp / ramp.sum(), "same") * np.sin(2 * np.pi * pitch * np.arange(len(key)) / rate)


def _hiss(n: int, seed: int = 2, level: float = 300.0) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n) * level


def _amp(snr_db: float, level: float = 300.0) -> float:
    """The tone's amplitude for a strength against the hiss in 2.5 kHz."""
    return float(np.sqrt(2 * level ** 2 / (RATE / 2) * 2500 * 10 ** (snr_db / 10)))


def _read_all(x: np.ndarray, pitch: float) -> str:
    env, er, noise = morse.noise_level(x, RATE, pitch)
    said = []
    for a, b in morse.overs(env, er, noise):
        seg = x[max(0, int((a - 1) * RATE)):int((b + 1) * RATE)]
        said.append(morse.decode(seg, RATE, pitch, noise)["text"])
    return " ".join(s for s in said if s)


def _speaker(x: np.ndarray) -> np.ndarray:
    """Audio at the reader's rate -> blocks as the station plays them (the speaker's rate, both channels)."""
    y = np.repeat(np.clip(x, -32768, 32767), morse.DECIMATE).astype(np.int16)
    return np.repeat(y[:, None], pcm.CHANNELS, axis=1)


def _played(x: np.ndarray) -> list[dict]:
    """Play x to a Reader a block at a time, stepping it as its thread would. -> the overs it read"""
    r = morse.Reader()
    r.on = True                                      # (no thread: stepped here)
    block = pcm.SAMPLE_RATE // 20
    sound, overs, seen = _speaker(x), [], 0
    for n, a in enumerate(range(0, len(sound), block)):
        r.feed(sound[a:a + block])
        if n % 5 == 4:
            r.step()
            for o in r.status(seen)["overs"]:
                overs.append(o)
                seen = o["n"]
    return overs


def _text(over: dict) -> str:
    return "".join(c[0] for c in over["chars"])


def test_a_clean_over_is_read_with_its_speed_and_its_marks() -> None:
    said = "CQ CQ DE M8ODJ M8ODJ K"
    x = _keyed(said, 22, 650) * 4000
    x = x + _hiss(len(x), level=20)
    env, er, noise = morse.noise_level(x, RATE, 655)                       # (asked for a few Hz off: it finds the tone)
    r = morse.decode(x, RATE, 655, noise)
    assert r["text"] == said and abs(r["wpm"] - 22) < 1.5 and abs(r["pitch"] - 650) < 3 and r["quality"] > 0.9
    assert len(r["marks"]) == sum(len(CODE[c]) for c in said if c != " ")
    letters = [c for c in r["chars"] if c[0] != " "]
    assert all(a[1] < b[1] for a, b in zip(letters, letters[1:])) and all(0 <= c[2] <= 1 for c in r["chars"])
    assert 0.9 < letters[0][1] < 1.1                                       # the first letter starts where the keying did


def test_weak_shaky_and_crowded_are_still_read() -> None:
    said = "DE G4ABC UR RST 579 NAME BOB QTH YORK"
    for wpm, shaky, snr in ((15, 0.0, -5), (25, 0.0, -4), (20, 0.15, 0), (32, 0.1, 3)):
        sig = _keyed(said, wpm, 700, shaky=shaky, seed=wpm) * _amp(snr)
        x = np.concatenate((_hiss(int(RATE * 4), seed=7), sig + _hiss(len(sig), seed=8), _hiss(int(RATE * 2), seed=9)))
        got = _read_all(x, 700)                      # (a shaky fist can close up a gap between words: the letters are what's asked)
        assert (got.replace(" ", "") == said.replace(" ", "") if shaky else got == said), (wpm, shaky, snr, got)
    # a station ten times as strong, 150 Hz up the band and at another speed: this one is still read
    sig = _keyed(said, 20, 700) * _amp(0)
    other = _keyed("CQ TEST DL1XYZ DL1XYZ TEST CQ TEST DL1XYZ DL1XYZ", 27, 850, lead=0.3)[:len(sig)] * _amp(10)
    sig[:len(other)] += other
    x = np.concatenate((_hiss(int(RATE * 4), seed=7), sig + _hiss(len(sig), seed=8), _hiss(int(RATE * 2), seed=9)))
    env, er, noise = morse.noise_level(x[:int(RATE * 4)], RATE, 700)
    got = morse.decode(x[int(RATE * 3):], RATE, 700, noise)["text"]
    assert said in got and set(got.replace(said, "")) <= set(" EIT")      # (its key clicks, while this one is silent, can add a stray dot)


def test_nothing_is_said_of_noise_or_of_a_teleprinter() -> None:
    assert _played(_hiss(int(RATE * 20))) == []
    # one tone of a teleprinter: keyed, but at 45 bits a second and to no letter's timing
    rng = np.random.default_rng(3)
    bits = np.repeat(rng.integers(0, 2, int(20 * 45.45)), int(RATE / 45.45))
    tone = bits * np.sin(2 * np.pi * 1000 * np.arange(len(bits)) / RATE) * 6000
    assert _played(tone + _hiss(len(tone))) == []
    # ...nor of a steady carrier
    assert _played(np.sin(2 * np.pi * 800 * np.arange(int(RATE * 16)) / RATE) * 6000 + _hiss(int(RATE * 16))) == []


def test_the_reader_gives_each_over_when_it_ends() -> None:
    first, second = _keyed("OH4X OH4X", 23, 700) * 5000, _keyed("5NN TU", 31, 730, lead=0.2) * 3000
    x = np.concatenate((_hiss(int(RATE * 3)), first + _hiss(len(first)), _hiss(int(RATE * 2)), second + _hiss(len(second)), _hiss(int(RATE * 3))))
    overs = _played(x)
    assert [_text(o) for o in overs] == ["OH4X OH4X", "5NN TU"]            # each with its own pitch and speed
    assert abs(overs[0]["pitch"] - 700) < 4 and abs(overs[1]["pitch"] - 730) < 4
    assert abs(overs[0]["wpm"] - 23) < 2 and abs(overs[1]["wpm"] - 31) < 2.5
    assert [o["n"] for o in overs] == [1, 2] and overs[0]["t1"] < overs[1]["t0"]
    assert abs(overs[0]["t0"] - 4.0) < 0.15                                # in seconds of audio heard: 3 s of hiss, a second's lead
    assert all(o["t0"] <= o["marks"][0][0] and o["marks"][-1][1] <= o["t1"] + 0.01 and o["quality"] > 0.8 for o in overs)


def test_a_long_over_is_read_in_parts_at_gaps_between_words() -> None:
    said = "CQ DE G4ABC UR RST 579 NAME BOB QTH YORK TNX FER CALL 73"
    sig = _keyed(said, 18, 700) * 4000
    x = np.concatenate((_hiss(int(RATE * 3)), sig + _hiss(len(sig)), _hiss(int(RATE * 3))))
    overs = _played(x)
    assert len(overs) > 2 and " ".join(_text(o) for o in overs) == said    # no word cut in two, none read twice
    assert all(a["t1"] < b["t0"] for a, b in zip(overs, overs[1:]))


def test_the_tape_and_the_switch() -> None:
    r = morse.Remote()                               # as the radio uses it: the reading in a process of its own
    r.feed(_speaker(_hiss(1000)))                    # off: nothing is kept
    assert r.status() == {"on": False, "at": 0.0, "pitch": None, "wpm": None, "env": [], "tape_rate": morse.TAPE_RATE, "overs": []}
    r.switch(True)
    try:
        proc = r._proc
        assert r.on and r._thread.is_alive() and proc.is_alive()
        sig = _keyed("TEST", 20, 600) * 5000
        r.feed(_speaker(np.concatenate((_hiss(int(RATE * 3)), sig + _hiss(len(sig))))))
        for _ in range(100):                         # the other process takes it in and says what it read
            if r.status()["at"] > 5 and r.status()["overs"]:
                break
            threading.Event().wait(0.1)
        st = r.status()
        assert st["on"] and st["pitch"] and abs(st["pitch"] - 600) < 8 and _text(st["overs"][0]) == "TEST"
        assert 0 < len(st["env"]) <= morse.TAPE_S * morse.TAPE_RATE and max(st["env"]) > 200 and min(st["env"]) < 30
        assert r.status(since=st["overs"][-1]["n"])["overs"] == []
    finally:
        thread = r._thread
        r.switch(False)
        thread.join(6)
    assert not r.on and not thread.is_alive() and not proc.is_alive() and r.status()["on"] is False
    r.switch(True)                                   # on again: from nothing
    try:
        assert r.status()["overs"] == [] and r.status()["at"] == 0.0
    finally:
        thread = r._thread
        r.switch(False)
        thread.join(6)


def test_a_reader_nobody_takes_from_keeps_only_so_much() -> None:
    r = morse.Reader()
    r.on = True
    for _ in range(morse.MAX_PENDING + 50):
        r.feed(_speaker(_hiss(400)))
    assert len(r._pending) == morse.MAX_PENDING and len(r.drain()) == morse.MAX_PENDING * 400 and r.drain() is None


def test_the_reader_from_the_web(tmp_path: Path) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("")
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
        assert call("/api/rx/morse")[1]["on"] is False
        assert call("/api/rx/morse", {"on": True}) == (200, {"cw": True}) and morse.reader.on
        for bad in ({"on": "yes"}, {"on": 1}, {}):
            assert call("/api/rx/morse", bad)[0] == 400 and morse.reader.on
        code, st = call("/api/rx/morse?since=3")
        assert code == 200 and st["on"] is True and st["overs"] == [] and st["tape_rate"] == morse.TAPE_RATE
        assert call("/api/rx/morse", {"on": False}) == (200, {"cw": False}) and not morse.reader.on
    finally:
        morse.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()
