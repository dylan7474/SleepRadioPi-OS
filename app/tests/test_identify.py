"""What's this?: a signal named from where it is, how it sounds and what the readers make of it (no network)."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import identify, morse, pcm, psk, rtty, sstv
from sleepradiopi.web.server import make_handler

RATE = identify.RATE
N = int(12.5 * RATE)
CODE = {v: k for k, v in morse.MORSE.items()}


def _hiss(n: int = N, seed: int = 2, level: float = 300.0) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n) * level


def _amp(snr_db: float, level: float = 300.0) -> float:
    """A tone's amplitude for a strength against the hiss in 2.5 kHz."""
    return float(np.sqrt(2 * level ** 2 / (RATE / 2) * 2500 * 10 ** (snr_db / 10)))


def _in_hiss(x: np.ndarray, snr_db: float = 15.0, seed: int = 2) -> np.ndarray:
    """A signal of amplitude 1, that strong, in N samples of hiss (cut or padded to fit)."""
    x = np.asarray(x[:N], float)
    return np.pad(x, (0, N - len(x))) * _amp(snr_db) + _hiss(seed=seed)


def _tone(pitch: float, n: int = N) -> np.ndarray:
    return np.sin(2 * np.pi * pitch * np.arange(n) / RATE)


def _morse(text: str, wpm: float, pitch: float) -> np.ndarray:
    dit, t, spans = 1.2 / wpm, 1.0, []
    for word in text.split(" "):
        for ch in word:
            for e in CODE[ch]:
                d = dit * (1 if e == "." else 3)
                spans.append((t, t + d))
                t += d + dit
            t += 2 * dit
        t += 4 * dit
    key = np.zeros(int((t + 1) * RATE))
    for a, b in spans:
        key[int(a * RATE):int(b * RATE)] = 1.0
    ramp = np.hanning(2 * int(RATE * 0.005) + 1)
    return np.convolve(key, ramp / ramp.sum(), "same") * _tone(pitch, len(key))


def _fsk(pitch: float, tones: int, apart: float, symbol_s: float, seed: int, seconds: float = 12.5) -> np.ndarray:
    """Tones in turn with no break between them, as FT8 (eight, 6.25 Hz apart, 0.16 s each) sends."""
    rng = np.random.default_rng(seed)
    per = int(symbol_s * RATE)
    f = np.repeat(pitch + apart * rng.integers(0, tones, int(seconds / symbol_s) + 1), per)[:int(seconds * RATE)]
    return np.sin(2 * np.pi * np.cumsum(f) / RATE)


def _talk(seed: int = 5) -> np.ndarray:
    """Something shaped like speech: syllables a fifth of a second long, each a band of noise somewhere else
    in the voice's range, with pauses between phrases."""
    rng = np.random.default_rng(seed)
    out, f = np.zeros(N), np.fft.rfftfreq(2048, 1 / RATE)
    t = int(0.3 * RATE)
    while t < N - 4096:
        if rng.random() < 0.15:                      # a breath
            t += int(rng.uniform(0.3, 0.7) * RATE)
            continue
        n = int(rng.uniform(0.12, 0.3) * RATE)
        centre, wide = rng.uniform(400, 1900), rng.uniform(150, 400)
        spec = np.fft.rfft(rng.standard_normal(2048)) * np.exp(-0.5 * ((f - centre) / wide) ** 2)
        syl = np.tile(np.fft.irfft(spec), n // 2048 + 1)[:n] * np.hanning(n)
        out[t:t + n] += syl[:max(0, min(n, N - t))] / (np.std(syl) + 1e-9)
        t += int(n * 0.8)
    return out


def _speaker(x: np.ndarray) -> np.ndarray:
    """Audio at the listener's rate -> what the station plays (the speaker's rate, both channels)."""
    y = np.repeat(np.clip(x, -32768, 32767), identify.DECIMATE).astype(np.int16)
    return np.repeat(y[:, None], pcm.CHANNELS, axis=1)


# --- where it is ---------------------------------------------------------------------------------

def test_whats_usually_there_the_one_frequency_before_the_part_of_the_band() -> None:
    kinds = lambda hz, mode: [p["kind"] for p in identify.places(hz, mode)]
    assert kinds(7_074_000, "usb")[:2] == ["ft8", "ham-phone"] or kinds(7_074_000, "usb")[0] == "ft8"
    assert kinds(7_038_600, "usb")[0] == "wspr" and "wspr" not in kinds(7_040_000, "usb")
    assert kinds(7_020_000, "usb") == ["ham-cw"]
    assert kinds(3_760_000, "lsb") == ["ham-phone"] and "ham-phone" in kinds(7_150_000, "lsb")
    assert kinds(10_099_300, "usb")[0] == "weather"              # (tuned 1500 Hz under its middle, as the page's key does)
    assert kinds(9_420_000, "am") == ["broadcast"] and kinds(198_000, "am") == ["broadcast"]
    assert kinds(7_878_100, "usb")[0] == "fax"
    assert kinds(5_505_000, "usb")[0] == "volmet" and "air" in kinds(5_505_000, "usb")
    assert kinds(145_500_000, "nfm") == ["ham-fm"] and kinds(156_800_000, "nfm") == ["sea-vhf"]
    assert kinds(8_500_123, "usb") == ["sea"] and kinds(23_500_000, "usb") == []
    # what's heard, on the air: above the dial in upper sideband, below it in lower
    assert identify.air(7_074_000, "usb", 1500) == 7_075_500 and identify.air(3_760_000, "lsb", 1000) == 3_759_000


# --- what it is ----------------------------------------------------------------------------------

def test_noise_is_only_noise_and_says_whose_place_it_is() -> None:
    r = identify.identify(_hiss(), 7_020_000, "usb")
    assert (r["kind"], r["sure"]) == ("noise", "nothing") and "Morse part of the 40 m amateur band" in r["says"] and "key" not in r
    r = identify.identify(_hiss(seed=9))             # (nobody said where it's tuned)
    assert r["kind"] == "noise" and r["places"] == []


def test_morse_is_read_by_the_morse_reader_and_its_frequency_given() -> None:
    r = identify.identify(_in_hiss(_morse("CQ CQ DE G4ABC G4ABC K", 20, 700)), 7_020_000, "usb")
    assert (r["kind"], r["sure"], r["key"]) == ("cw", "read", "cw") and "G4ABC" in r["text"]
    assert abs(r["at"] - 7_020_700) < 20 and "20 words a minute" in r["says"] and r["says"].endswith("The CW key reads it.")
    # in lower sideband it's below the dial
    r = identify.identify(_in_hiss(_morse("CQ CQ DE G4ABC G4ABC K", 20, 700)), 7_020_000, "lsb")
    assert r["kind"] == "cw" and abs(r["at"] - 7_019_300) < 20


def test_a_teleprinter_frames_and_the_weather_service_is_known_by_its_place() -> None:
    x = _in_hiss(rtty.send("GALE WARNING NORTH SEA WIND FORCE 8 RYRYRYRYRYRY GALE WARNING", RATE, 1275.0, 450.0, 50.0), 18)
    r = identify.identify(x, 10_099_300, "usb")
    assert (r["kind"], r["sure"], r["key"]) == ("rtty", "read", "rtty") and "German weather service" in r["says"] and "GALE" in r["text"]
    assert "450 Hz apart" in r["says"] and abs(r["at"] - 10_100_800) < 30
    r = identify.identify(_in_hiss(rtty.send("CQ CQ DE G4ABC G4ABC G4ABC PSE K CQ CQ DE G4ABC", RATE, 1000.0, 170.0, 45.45), 18), 14_085_000, "usb")
    assert r["kind"] == "rtty" and "German" not in r["says"] and "170 Hz apart" in r["says"]


def test_psk31_says_enough_letters() -> None:
    r = identify.identify(_in_hiss(psk.send("cq cq de g4abc g4abc pse k cq cq de g4abc pse k", RATE, 1000.0), 12), 14_070_000, "usb")
    assert (r["kind"], r["sure"], r["key"]) == ("psk31", "read", "psk31") and "g4abc" in r["text"] and "PSK31" in r["says"]


def test_a_picture_is_known_by_its_header() -> None:
    img = (np.random.default_rng(3).random((256, 320, 3)) * 255).astype(np.uint8)
    r = identify.identify(_in_hiss(sstv.send(60, img, RATE), 20), 14_230_000, "usb")
    assert (r["kind"], r["sure"], r["key"]) == ("sstv", "read", "sstv") and "Scottie S1" in r["says"]


def test_ft8_ft4_and_wspr_are_known_by_their_frequencies_and_their_width() -> None:
    ft8 = _in_hiss(sum(_fsk(p, 8, 6.25, 0.16, i) for i, p in enumerate((700.0, 1300.0, 2000.0))), 10)
    r = identify.identify(ft8, 7_074_000, "usb")
    assert (r["kind"], r["sure"], r["key"]) == ("ft8", "likely", "ft8") and "3 signals" in r["says"]
    # the same sound somewhere that isn't FT8's: the width is said, and no more claimed
    r = identify.identify(ft8, 7_150_000, "usb")
    assert (r["kind"], r["sure"]) == ("data", "guess") and "key" not in r
    ft4 = _in_hiss(sum(_fsk(p, 4, 20.83, 0.048, i) for i, p in enumerate((800.0, 1700.0))), 10)
    assert identify.identify(ft4, 7_047_500, "usb")["kind"] == "ft4"
    wspr = _in_hiss(_fsk(1500.0, 4, 1.46, 0.683, 1), 5)
    r = identify.identify(wspr, 7_038_600, "usb")
    assert (r["kind"], r["key"]) == ("wspr", "wspr") and abs(r["at"] - 7_040_100) < 30
    # (where the two overlap, on 80 m, it's the one more of what's heard is in)
    both = _in_hiss(sum(_fsk(p, 8, 6.25, 0.16, i) for i, p in enumerate((500.0, 900.0, 1400.0, 2400.0))), 10)
    assert identify.identify(both, 3_573_000, "usb")["kind"] == "ft8"


def test_a_steady_tone_is_a_carrier_a_time_signal_or_a_chart_service_by_where_it_is() -> None:
    whistle = _in_hiss(_tone(1000.0), 10)
    r = identify.identify(whistle, 14_200_000, "usb")
    assert (r["kind"], r["sure"]) == ("carrier", "likely") and abs(r["at"] - 14_201_000) < 20
    r = identify.identify(whistle, 6_069_000, "usb")
    assert r["kind"] == "carrier" and "broadcast station's carrier" in r["says"] and "AM key" in r["says"]
    r = identify.identify(whistle, 9_999_000, "usb")
    assert r["kind"] == "time" and "WWV" in r["says"]
    r = identify.identify(_in_hiss(_tone(2300.0), 15), 7_878_100, "usb")
    assert (r["kind"], r["key"]) == ("fax", "fax") and "between charts" in r["says"] and "Hamburg" in r["says"]


def test_speech_swells_and_wanders_and_is_named_by_where_it_is() -> None:
    talk = _talk() * 1500 + _hiss(level=120.0)
    r = identify.identify(talk, 3_760_000, "lsb")
    assert (r["kind"], r["sure"]) == ("speech", "likely") and "radio amateurs talking" in r["says"] and "other sideband" in r["says"]
    r = identify.identify(talk, 909_000, "am")
    assert r["kind"] == "broadcast" and "medium wave" in r["says"]
    r = identify.identify(talk, 6_070_000, "usb")
    assert r["kind"] == "broadcast" and "AM key" in r["says"]
    r = identify.identify(talk, 156_800_000, "nfm")
    assert r["kind"] == "speech" and "ships" in r["says"]
    # static crashes swell and wander too, but fill nothing: not speech
    rng = np.random.default_rng(4)
    crashes = _hiss() * (1 + 3 * (np.repeat(rng.random(N // 700 + 1), 700)[:N] > 0.85))
    assert identify.identify(crashes, 3_760_000, "lsb")["kind"] != "speech"


def test_a_block_of_data_and_what_cant_be_named_are_said_as_guesses() -> None:
    f = np.fft.rfftfreq(N, 1 / RATE)
    block = np.fft.irfft(np.fft.rfft(_hiss(seed=7)) * ((f > 800) & (f < 2000)), N) * 8 + _hiss()
    r = identify.identify(block, 8_300_000, "usb")
    assert (r["kind"], r["sure"]) == ("data", "guess") and "1200 Hz wide" in r["says"] and "ships and coast stations" in r["says"]
    keyed = _in_hiss(_tone(900.0) * (np.random.default_rng(1).random(N // 300 + 1).repeat(300)[:N] > 0.5), 15)       # keyed any old how
    r = identify.identify(keyed, 8_300_000, "usb")
    assert (r["kind"], r["sure"]) == ("keyed", "guess")
    # an AM or FM detector gives nothing to go by but speech: the place is said
    r = identify.identify(_hiss(), 9_420_000, "am")
    assert r["kind"] == "noise" and "31 m broadcast band" in r["says"]


# --- the one the radio uses ----------------------------------------------------------------------

def _wait(listener, states=("done", "failed"), timeout: float = 30.0) -> dict:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        st = listener.status()
        if st["state"] in states:
            return st
        time.sleep(0.05)
    raise AssertionError(f"still {listener.status()['state']}")


def test_the_listener_takes_twelve_seconds_of_what_is_played_and_names_it() -> None:
    lis = identify.Listener(own_process=False)
    assert lis.status()["state"] == "idle"
    blocks = _speaker(_in_hiss(_morse("CQ CQ DE G4ABC G4ABC K", 20, 700)))
    lis.feed(blocks[:4096])                          # (not listening: nothing is kept)
    st = lis.start(7_020_000, "usb")
    assert st["state"] == "listening" and st["left_s"] == identify.LISTEN_S and st["n"] == 1
    for i in range(0, len(blocks), 4096):
        lis.feed(blocks[i:i + 4096])
    st = _wait(lis)
    assert st["state"] == "done" and st["result"]["kind"] == "cw" and "G4ABC" in st["result"]["text"]
    json.dumps(st)                                   # (it goes to the page as it is)
    # a mode the radio has another name for, or none: not a sideband
    assert lis.start(145_500_000, "nbfm")["mode"] == "nfm" and lis.start(None, "drm")["mode"] == "am" and lis.status()["n"] == 3
    with pytest.raises(ValueError):
        lis.start("7 MHz", "usb")


def test_with_nothing_played_it_says_so(monkeypatch) -> None:
    monkeypatch.setattr(identify, "STARVED_S", 0.3)
    lis = identify.Listener(own_process=False)
    lis.start(7_020_000, "usb")
    st = _wait(lis, timeout=5)
    assert st["state"] == "failed" and "is the radio playing it" in st["error"]
    lis.start(145_500_000, "nfm")                    # a squelched channel: silence, played
    silent = np.zeros((4096, pcm.CHANNELS), np.int16)
    for _ in range(int(identify.LISTEN_S * pcm.SAMPLE_RATE / 4096) + 2):
        lis.feed(silent)
    st = _wait(lis, timeout=5)
    assert st["state"] == "failed" and "silent" in st["error"]


def test_the_naming_is_done_in_a_process_of_its_own() -> None:
    lis = identify.Listener()
    lis.start(10_099_300, "usb")
    blocks = _speaker(_in_hiss(rtty.send("GALE WARNING NORTH SEA WIND FORCE 8 RYRYRYRYRYRY GALE WARNING", RATE, 1275.0, 450.0, 50.0), 18))
    for i in range(0, len(blocks), 4096):
        lis.feed(blocks[i:i + 4096])
    st = _wait(lis, timeout=60)
    assert st["state"] == "done" and st["result"]["kind"] == "rtty"


def test_whats_this_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    monkeypatch.setattr(identify, "listener", identify.Listener(own_process=False))
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
        assert call("/api/rx/identify") == (200, {"state": "idle", "n": 0, "hz": None, "mode": "usb", "left_s": 0.0})
        assert call("/api/rx/identify", {"hz": "seven", "mode": "usb"})[0] == 400 and call("/api/rx/identify", [1])[0] == 400
        code, st = call("/api/rx/identify", {"hz": 7_074_000, "mode": "usb"})
        assert code == 200 and st["state"] == "listening" and st["hz"] == 7_074_000
        blocks = _speaker(_in_hiss(sum(_fsk(p, 8, 6.25, 0.16, i) for i, p in enumerate((700.0, 1300.0, 2000.0))), 10))
        for i in range(0, len(blocks), 4096):
            identify.listener.feed(blocks[i:i + 4096])
        _wait(identify.listener)
        code, st = call("/api/rx/identify")
        assert code == 200 and st["state"] == "done" and st["result"]["kind"] == "ft8" and st["result"]["says"].endswith("The FT8 key reads it.")
    finally:
        httpd.shutdown()
        httpd.server_close()
