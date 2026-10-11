"""SSTV and weather fax for receivers (audio/sstv.py, audio/pictures.py)."""
import json
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm, pictures, sstv
from sleepradiopi.web.server import make_handler

RATE = pictures.RATE


def _card(w: int, h: int) -> np.ndarray:
    """A test card: colour bars over a grey ramp."""
    img = np.zeros((h, w, 3), np.uint8)
    for i, c in enumerate([(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]):
        img[:h // 2, i * w // 8:(i + 1) * w // 8] = c
    img[h // 2:] = (np.arange(w) * 255 // (w - 1)).astype(np.uint8)[None, :, None]
    return img


def _noise(n: float, level: float = 200.0, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, level, int(n * RATE))


def _followed(mode: str, x: np.ndarray, orders: dict[int, str] | None = None) -> tuple[dict[int, tuple[str, np.ndarray, bool]], list[str], pictures.Reader]:
    """Play x (at the reader's rate) to a Reader a step at a time. -> ({n: (name, picture, done)}, the states it went through, the reader)"""
    r = pictures.Reader(mode)
    r.on = True                                      # (no thread: stepped here)
    got: dict[int, dict] = {}
    states: list[str] = []
    step = int(RATE * pictures.STEP_S)
    for k, i in enumerate(range(0, len(x), step)):
        if orders and k in orders:
            r.order(orders[k])
        r.take(x[i:i + step])
        r.step()
        for item in r.news():
            if "state" in item:
                states.append(item["state"])
                continue
            p = got.setdefault(item["n"], {"name": item["name"], "w": item["w"], "grey": item["grey"], "rows": bytearray(), "done": False})
            assert item["from"] * p["w"] * (1 if p["grey"] else 3) == len(p["rows"])       # each line once, in order
            p["rows"] += item["rows"]
            p["done"] = item["done"]
    return ({n: (p["name"], np.frombuffer(bytes(p["rows"]), np.uint8).reshape((-1, p["w"]) if p["grey"] else (-1, p["w"], 3)), p["done"]) for n, p in got.items()},
            states, r)


def _off(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(int) - b.astype(int)).mean())


# --- SSTV ------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", [44, 40, 60, 56, 8, 99, 95])
def test_each_mode_comes_back_as_it_was_sent(code: int) -> None:
    m = sstv.MODES[code]
    img = _card(m["w"], m["h"])
    x = sstv.send(code, img, RATE) * 3000
    assert abs(len(x) / RATE - (0.5 + 0.91 + sstv.picture_s(m) + 0.3)) < 0.05          # (as long as the mode says)
    got = sstv.decode(x + np.random.default_rng(1).normal(0, 60.0, len(x)), RATE)
    assert got is not None and got[0] == m["name"] and got[1].shape == img.shape
    assert _off(got[1], img) < (12 if m["kind"] in ("pd", "robot36") else 6), m["name"]       # (colour sent at half the detail: a little less exact)


def test_no_header_in_noise_or_a_voice() -> None:
    assert sstv.decode(_noise(20.0, 1000.0), RATE) is None
    t = np.arange(int(20 * RATE)) / RATE
    voice = 2000 * np.sin(2 * np.pi * (700 + 500 * np.sin(2 * np.pi * 3 * t)) * t) + _noise(20.0, 300.0, 2)
    assert sstv.decode(voice, RATE) is None
    assert sstv.find_vis(np.full(100, 1900.0), RATE) is None                           # (too little of it to say)


def test_a_transmitter_whose_clock_is_out_doesnt_slant_the_picture() -> None:
    img = _card(320, 256)
    x = sstv.send(60, img, RATE * 1.0006) * 3000     # sent 600 parts in a million slow: a tenth of the picture's width by the last line, unless followed
    got = sstv.decode(x, RATE)
    assert got is not None and _off(got[1][-40:], img[-40:]) < 12


def test_pictures_are_read_as_they_arrive_one_after_another() -> None:
    a, b = _card(320, 256), _card(320, 240)
    x = np.concatenate((_noise(3.3), sstv.send(60, a, RATE) * 3000, _noise(4.0, seed=2), sstv.send(8, b, RATE) * 3000, _noise(3.0, seed=3)))
    got, states, r = _followed("sstv", x + np.random.default_rng(4).normal(0, 100.0, len(x)))
    assert [(n, name, pic.shape, done) for n, (name, pic, done) in got.items()] == [(1, "Scottie S1", (256, 320, 3), True), (2, "Robot 36", (240, 320, 3), True)]
    assert _off(got[1][1], a) < 6 and _off(got[2][1], b) < 12
    assert states == ["receiving", "listening", "receiving", "listening"]
    assert len(r._f) < 8 * RATE                      # (the pitch kept doesn't grow: a header is a second long)


def test_a_picture_cut_short_is_kept_as_far_as_it_got() -> None:
    x = np.concatenate((_noise(2.0), sstv.send(44, _card(320, 256), RATE) * 3000))
    got, states, _ = _followed("sstv", x, orders={40: "end"})
    (name, pic, done), = got.values()
    assert name == "Martin M1" and done and 60 < pic.shape[0] < 100 and states == ["receiving", "listening"]


# --- weather fax -----------------------------------------------------------------------------

def _chart(lines: int = 100) -> np.ndarray:
    chart = np.full((lines, 512), 255, np.uint8)
    chart[lines // 5:lines * 4 // 5, 100:400] = 0
    chart[lines * 2 // 5:lines * 3 // 5, 150:350] = 128
    chart[:, 30:34] = 0
    return chart


def test_a_chart_from_its_start_tone_to_its_stop_tone() -> None:
    chart = _chart()
    x = np.concatenate((_noise(2.3), sstv.send_fax(chart, RATE, phasing_s=20.0) * 3000, _noise(4.0, seed=2)))
    got, states, r = _followed("fax", x + np.random.default_rng(3).normal(0, 100.0, len(x)))
    (name, pic, done), = got.values()
    assert name == "Weather fax" and done and pic.shape[1] == sstv.FAX_WIDTH and states == ["announced", "receiving", "listening"]
    lead = pic.shape[0] - len(chart)                 # (the phasing lines not used for finding the line's start come first)
    assert 10 <= lead <= 40 and _off(pic[lead:], chart[:, np.arange(sstv.FAX_WIDTH) * 512 // sstv.FAX_WIDTH]) < 16       # the lines begin where the chart's do
    assert len(r._f) < 12 * RATE


def test_a_chart_under_way_is_begun_by_hand() -> None:
    chart = _chart(60)
    f = np.concatenate([1500.0 + chart[k][(np.arange(int(0.5 * RATE)) * 512 // int(0.5 * RATE))] / 255.0 * 800.0 for k in range(60)])
    x = sstv._tone(f, RATE) * 3000
    got, states, _ = _followed("fax", x, orders={3: "begin", 25: "end"})
    (name, pic, done), = got.values()
    assert done and 35 <= pic.shape[0] <= 48 and states == ["receiving", "listening"]
    assert got and pictures.Reader("sstv").order("begin") is None                       # (not for SSTV: it's ignored)


def test_the_tones_and_the_phasing_lines() -> None:
    sq = lambda hz: sstv._tone(np.where(np.sin(2 * np.pi * hz * np.arange(int(1.2 * RATE)) / RATE) >= 0, 2300.0, 1500.0), RATE)
    pitch = lambda x: sstv.pitch(x * 3000 + _noise(1.2, 100.0), RATE)[0][200:-200]
    assert sstv.fax_tone(pitch(sq(300.0)), RATE) == "start" and sstv.fax_tone(pitch(sq(450.0)), RATE) == "stop"
    assert sstv.fax_tone(pitch(sq(380.0)), RATE) is None and sstv.fax_tone(sstv.pitch(_noise(1.2, 1000.0), RATE)[0], RATE) is None
    line = np.full(int(0.5 * RATE), 2300.0)
    line[:len(line) // 20] = 1500.0
    for cut in (0.0, 0.2, 0.49):                     # wherever the listening began in the line
        f = np.tile(line, 20)[int(cut * RATE):]
        begins = sstv.fax_phase(f[:int(8 * RATE)], RATE)
        assert begins is not None and min(abs(begins - (0.5 - cut) % 0.5), 0.5 - abs(begins - (0.5 - cut) % 0.5)) < 0.006
    assert sstv.fax_phase(np.full(int(8 * RATE), 2300.0), RATE) is None and sstv.fax_phase(sstv.pitch(_noise(8.0, 1000.0), RATE)[0], RATE) is None


# --- as the radio has it ---------------------------------------------------------------------

def _png(data: bytes) -> tuple[int, int, int, bytes]:
    """A PNG file -> (width, height, colour type, the lines as they were)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, {}
    while at < len(data):
        n, kind = struct.unpack(">I4s", data[at:at + 8])
        body = data[at + 8:at + 8 + n]
        assert struct.unpack(">I", data[at + 8 + n:at + 12 + n])[0] == zlib.crc32(kind + body)
        chunks[kind] = chunks.get(kind, b"") + body
        at += 12 + n
    w, h, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert depth == 8
    return w, h, colour, zlib.decompress(chunks[b"IDAT"])


def test_a_png_file() -> None:
    img = _card(32, 20)
    w, h, colour, raw = _png(pictures.png(img))
    assert (w, h, colour) == (32, 20, 2) and len(raw) == 20 * (1 + 32 * 3)
    assert np.frombuffer(raw, np.uint8).reshape(20, 97)[:, 1:].reshape(20, 32, 3).tolist() == img.tolist()
    grey = (np.arange(50 * 8) % 256).astype(np.uint8).reshape(8, 50)
    w, h, colour, raw = _png(pictures.png(grey))
    assert (w, h, colour) == (50, 8, 0) and np.frombuffer(raw, np.uint8).reshape(8, 51)[:, 1:].tolist() == grey.tolist()
    assert _png(pictures.png(np.zeros((0, 16), np.uint8)))[:2] == (16, 1)               # (nothing read yet: a line of black, not no file)


def test_the_process_of_its_own_and_the_web(tmp_path: Path) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, raw=False):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, (r.read() if raw else json.loads(r.read()))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    img = _card(320, 240)
    x = np.concatenate((_noise(1.5), sstv.send(8, img, RATE) * 3000, _noise(2.0, seed=2)))
    played = np.repeat(np.repeat(np.clip(x, -32768, 32767), pictures.DECIMATE).astype(np.int16)[:, None], pcm.CHANNELS, axis=1)
    try:
        assert call("/api/rx/pictures")[1] == {"on": False, "mode": "sstv", "state": "listening", "pictures": []}
        for bad in ({"on": "yes"}, {"on": True, "mode": "rtty"}, {}):
            assert call("/api/rx/pictures", bad)[0] == 400
        assert call("/api/rx/pictures", {"on": True}) == (200, {"cw": False, "ft8": None, "keyboard": None, "pictures": "sstv"})
        for i in range(0, len(played), 4410):        # (as the station plays it, but quicker than the clock)
            pictures.reader.feed(played[i:i + 4410])
            time.sleep(0.004)
        until = time.time() + 30
        while time.time() < until and not any(p["done"] for p in call("/api/rx/pictures")[1]["pictures"]):
            time.sleep(0.3)
        st = call("/api/rx/pictures")[1]
        assert st["on"] and st["state"] == "listening" and len(st["pictures"]) == 1
        p = st["pictures"][0]
        assert (p["name"], p["w"], p["h"], p["lines"], p["done"]) == ("Robot 36", 320, 240, 240, True)
        code, data = call(f"/api/rx/pictures/{p['n']}.png", raw=True)
        w, h, colour, raw = _png(data)
        assert code == 200 and (w, h, colour) == (320, 240, 2)
        assert _off(np.frombuffer(raw, np.uint8).reshape(240, 961)[:, 1:].reshape(240, 320, 3), img) < 12
        assert call("/api/rx/pictures/99.png")[0] == 400 and call("/api/rx/pictures/x.png")[0] == 400
        assert call("/api/rx/pictures", {"on": True, "mode": "fax"})[1]["pictures"] == "fax"
        assert [q["n"] for q in call("/api/rx/pictures")[1]["pictures"]] == [p["n"]]      # (the other mode: the pictures read stay)
        assert call("/api/rx/pictures", {"begin": True})[0] == 200 and call("/api/rx/pictures", {"end": True})[0] == 200
        assert call("/api/rx/pictures", {"on": False})[1]["pictures"] is None and not pictures.reader.on
    finally:
        pictures.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()
