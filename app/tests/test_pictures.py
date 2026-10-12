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
            assert item["from"] * p["w"] * (1 if p["grey"] else 3) <= len(p["rows"])       # each line in order (one sent again takes the place of what follows)
            del p["rows"][item["from"] * p["w"] * (1 if p["grey"] else 3):]
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
    wide = chart[:, np.arange(sstv.FAX_WIDTH) * 512 // sstv.FAX_WIDTH]
    # (the phasing lines not used for finding the line's start come first; the last few lines, read off the stop tone, are taken away)
    lead = min(range(10, 41), key=lambda k: _off(pic[k:k + 90], wide[:len(pic[k:k + 90])]))
    assert _off(pic[lead:lead + 90], wide[:len(pic[lead:lead + 90])]) < 16 and len(chart) - 6 <= len(pic) - lead <= len(chart)       # the lines begin where the chart's do
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


# --- SSTV joined late: no header, only its sync pulses ----------------------------------------

@pytest.mark.parametrize("code", [44, 40, 60, 56, 76, 8, 93, 99, 95])
def test_a_picture_joined_late_is_read_from_its_sync_pulses(code: int) -> None:
    m = sstv.MODES[code]
    img = _card(m["w"], m["h"])
    img[:, :, 2] = (np.arange(m["h"]) * 255 // (m["h"] - 1)).astype(np.uint8)[:, None]     # (blue by the line: which lines came back shows)
    whole = sstv.send(code, img, RATE) * 3000
    header = int((0.5 + 0.91) * RATE)
    missed = 21 if code != 8 else 22                 # joined this many lines in (Robot 36: on an even line, then on an odd one)
    for extra in ((0, 1) if code == 8 else (0,)):
        sent = missed + extra
        cut = header + int((sent + 0.3) * sstv.line_s(m) * RATE)       # ...and part of the way through one
        tail = sstv.lines_sent(m) - sent - 1
        if code in (76, 95):                         # (the slow ones: a dozen lines of them is enough to tell)
            tail = 12
        x = np.concatenate((whole[cut:cut + int((tail + 0.75) * sstv.line_s(m) * RATE)], _noise(max(4.0, 10 * sstv.line_s(m)), seed=4)))
        got, states, r = _followed("sstv", x + np.random.default_rng(5).normal(0, 150.0, len(x)))
        (name, pic, done), = got.values()            # one picture, not a second begun from the first's last lines
        per = 2 if m["kind"] == "pd" else 1
        assert name == m["name"] + " (joined late)" and done and states == ["receiving", "listening"], (m["name"], states)
        assert abs(len(pic) - tail * per) <= 2 * per, (m["name"], len(pic), tail * per)     # it ended where the pulses did
        first = (sent + 1) * per                     # the first whole line it could have had
        want = img[first:first + len(pic)]
        n = min(len(pic), len(want)) - 2 * per
        assert n > 8 and _off(pic[per:n], want[per:n]) < 14, (m["name"], extra, _off(pic[per:n], want[per:n]))


def test_no_picture_is_begun_from_noise_a_voice_or_a_warble() -> None:
    t = np.arange(int(RATE * 20)) / RATE
    rng = np.random.default_rng(6)
    voice = 3000 * np.sin(2 * np.pi * (700 + 500 * np.sin(2 * np.pi * 2.3 * t)) * t) * (np.sin(2 * np.pi * 1.1 * t) > -0.3)
    warble = 3000 * np.sin(2 * np.pi * np.cumsum(1200 + 300 * np.sign(np.sin(2 * np.pi * 3 * t))) / RATE)      # down at the sync's pitch three times a second
    pips = 3000 * np.sin(2 * np.pi * np.cumsum(np.where((t % 0.4464) < 0.005, 1200.0, 1200.0)) / RATE)            # a steady tone at the sync's pitch
    for x in (rng.normal(0, 1000, len(t)), voice, warble, pips):
        f, _ = sstv.pitch(x + rng.normal(0, 100, len(t)), RATE)
        for a in range(0, len(f) - int(7 * RATE), int(RATE)):
            assert sstv.find_late(f[a:a + int(7 * RATE)], RATE) is None
        assert _followed("sstv", x)[0] == {}


def test_a_picture_from_its_header_is_not_followed_by_another_from_its_last_lines() -> None:
    img = _card(320, 240)
    x = np.concatenate((_noise(1.5), sstv.send(8, img, RATE) * 3000, _noise(9.0, seed=2)))
    got, states, r = _followed("sstv", x)
    (name, pic, done), = got.values()
    assert name == "Robot 36" and done and pic.shape == (240, 320, 3) and states == ["receiving", "listening"]


# --- Hellschreiber: text sent as a picture of itself -------------------------------------------

def _tape(mode_x: np.ndarray) -> tuple[np.ndarray | None, list[str], pictures.Reader, int]:
    """Play sound to a Hellschreiber reader. -> (the tape, the states it went through, the reader, how many tapes)"""
    r = pictures.Reader("hell")
    r.on = True
    rows: dict[int, bytearray] = {}
    states, step = [], int(RATE * pictures.STEP_S)
    for i in range(0, len(mode_x), step):
        r.take(mode_x[i:i + step])
        r.step()
        for item in r.news():
            if "state" in item:
                states.append(item["state"])
                continue
            assert item["name"] == "Hellschreiber" and item["grey"] and item["w"] == sstv.HELL_WIDTH
            tape = rows.setdefault(item["n"], bytearray())
            del tape[item["from"] * item["w"]:]      # (the strip being printed comes afresh each time)
            tape += item["rows"]
    first = next(iter(rows.values()), None)
    return (None if first is None else np.frombuffer(bytes(first), np.uint8).reshape(-1, sstv.HELL_WIDTH)), states, r, len(rows)


@pytest.mark.parametrize("pitch", [700.0, 980.0, 1900.0])
def test_hellschreiber_is_printed_as_it_was_sent(pitch: float) -> None:
    dots = np.zeros((7, 120), bool)
    for c in range(120):
        dots[c % 7, c] = True                        # a diagonal, over and over...
        if c % 20 < 3:
            dots[:, c] = True                        # ...and a bar every twenty columns
    rng = np.random.default_rng(1)
    x = sstv.send_hell(dots, RATE, pitch) * 3000
    x = np.concatenate((rng.normal(0, 150, int(RATE * 3)), x + rng.normal(0, 150, len(x)), rng.normal(0, 150, int(RATE * 14))))
    tape, states, r, tapes = _tape(x)
    assert tapes == 1 and states == ["receiving", "listening"] and abs(r._tone - pitch) < 15
    assert tape.shape == (sstv.HELL_STRIP_LINES, sstv.HELL_WIDTH) and (tape[-sstv.HELL_GAP:] == 255).all()
    lines = sstv.HELL_COLUMN * sstv.HELL_SCALE
    assert _off(tape[:lines], tape[lines:2 * lines]) == 0                 # printed twice, one above the other
    ink = tape[:lines:sstv.HELL_SCALE, ::sstv.HELL_SCALE] < 128           # one of them, a dot a cell: fourteen by a strip's columns
    cols = np.flatnonzero(ink.any(axis=0))
    assert 118 <= len(cols) <= 124 and cols[-1] - cols[0] <= 124          # as many columns as were sent, and no blank tape after them
    bars = np.flatnonzero(ink.mean(axis=0) > 0.9)                         # the bars: three columns wide, twenty apart
    assert len(bars) >= 15 and set(np.diff(bars)) <= {1, 2, 17, 18, 19}
    between = ink[:, bars[2] + 3:bars[2] + 15]                            # the diagonal between two bars: a dot (two halves, so two cells
    counts = between.sum(axis=0)                                          # or three as the timing falls) in each column...
    assert ((counts >= 1) & (counts <= 3)).mean() > 0.9
    # ...each a step down from the one before, as it was sent (or round the edge): the middle of its ink, on the ring of fourteen
    mid = [float(np.angle(np.sum(np.exp(2j * np.pi * np.flatnonzero(between[:, k]) / sstv.HELL_COLUMN))) / (2 * np.pi) * sstv.HELL_COLUMN) for k in range(between.shape[1]) if counts[k]]
    steps = [(b - a) % sstv.HELL_COLUMN for a, b in zip(mid, mid[1:])]
    assert sum(1 for d in steps if 1.0 <= d <= 3.0) >= len(steps) - 2


def test_hellschreiber_prints_nothing_of_noise_or_a_steady_tone_and_a_long_over_is_several_strips() -> None:
    rng = np.random.default_rng(2)
    t = np.arange(int(RATE * 30)) / RATE
    for x in (rng.normal(0, 1000, len(t)), 3000 * np.sin(2 * np.pi * 980 * t) + rng.normal(0, 150, len(t))):
        tape, states, r, tapes = _tape(x)
        assert tapes == 0 and states == []
    dots = rng.random((7, 800)) < 0.35               # 46 seconds of it: two strips and a half
    x = sstv.send_hell(dots, RATE, 1200.0) * 3000
    x = np.concatenate((x + rng.normal(0, 150, len(x)), rng.normal(0, 150, int(RATE * 12))))
    tape, states, r, tapes = _tape(x)
    assert tapes == 1 and states == ["receiving", "listening"] and tape.shape[0] == 3 * sstv.HELL_STRIP_LINES
    ink = tape[::sstv.HELL_SCALE, ::sstv.HELL_SCALE] < 128
    assert 0.2 < ink[:14].mean() < 0.5 and ink[2 * 31:2 * 31 + 14, 250:].mean() < 0.02        # full strips of it, and the last one blank past where it stopped


# --- afterwards: kept on the card, cleaned, slid straight -------------------------------------

def test_a_png_file_is_read_back_as_it_was_written() -> None:
    rng = np.random.default_rng(1)
    for a in (rng.integers(0, 255, (40, 64), np.uint8), rng.integers(0, 255, (30, 50, 3), np.uint8), np.zeros((1, 8), np.uint8)):
        back = pictures.unpng(pictures.png(a))
        assert back.shape == a.shape and (back == a).all()
    for bad in (b"not a png", pictures.png(np.zeros((4, 4), np.uint8))[:40], b"\x89PNG\r\n\x1a\n" + b"\0" * 30):
        with pytest.raises((ValueError, Exception)):
            pictures.unpng(bad)


def test_speckle_is_taken_out_and_the_picture_left() -> None:
    card = _card(160, 120)
    rng = np.random.default_rng(2)
    spots = rng.random(card.shape[:2]) < 0.04        # one dot in twenty-five gone to white or black
    speckled = card.copy()
    speckled[spots] = rng.choice([0, 255], (int(spots.sum()), 1))
    cleaned = pictures.clean(speckled)
    assert cleaned.shape == card.shape and cleaned.dtype == np.uint8
    assert _off(speckled, card) > 4 and _off(cleaned, card) < 0.6       # the dots gone; the bars' edges where they were
    grey = pictures.clean(speckled[:, :, 0])
    assert grey.shape == (120, 160) and _off(grey, card[:, :, 0]) < 0.6
    assert pictures.clean(np.zeros((2, 5), np.uint8)).shape == (2, 5)   # (too small to have a middle: left)


def _finished(r: pictures.Remote, n: int, name: str, a: np.ndarray, done: bool = True) -> None:
    """Hand a Remote a picture as its reader would have."""
    with r._lock:
        p = r._take({"n": n, "name": name, "w": a.shape[1], "h": 0 if a.ndim == 2 else a.shape[0], "grey": a.ndim == 2, "from": 0, "rows": a.tobytes(), "done": done}, 0)
    if p is not None:
        r._save(p)


def test_pictures_are_kept_on_the_card_over_a_restart(tmp_path: Path) -> None:
    folder = tmp_path / "state" / "pictures"
    r = pictures.Remote()
    r.keep_in(folder)                                # nothing there yet
    assert r.status()["pictures"] == [] and not folder.exists()
    card, chart = _card(320, 240), _chart(80)
    _finished(r, 1, "Robot 36", card)
    _finished(r, 2, "Weather fax", chart)
    _finished(r, 3, "Martin M1", card[:100], done=False)        # (one still coming: not on the card yet)
    st = r.status()["pictures"]
    assert [(p["name"], p["kept"], p["done"], p["grey"]) for p in st] == [("Robot 36", True, True, False), ("Weather fax", True, True, True), ("Martin M1", False, False, False)]
    assert sorted(f.name for f in folder.iterdir() if f.suffix == ".png").__len__() == 2 and len(json.loads((folder / pictures.INDEX).read_text())["pictures"]) == 2
    again = pictures.Remote()                        # the radio started again: the two finished ones are back, as they were
    again.keep_in(folder)
    back = again.status()["pictures"]
    assert [(p["name"], p["w"], p["h"], p["lines"], p["done"], p["kept"]) for p in back] == [("Robot 36", 320, 240, 240, True, True), ("Weather fax", 512, 80, 80, True, True)]
    assert (again.picture(back[0]["n"]) == card).all() and (again.picture(back[1]["n"]) == chart).all()
    assert abs(back[0]["t"] - st[0]["t"]) < 1
    for k in range(pictures.KEPT):                   # more than are kept: the oldest go, from the card too
        _finished(again, 10 + k, "Scottie S1", card)
    assert len(again.status()["pictures"]) == pictures.KEPT and len(list(folder.glob("picture-*.png"))) == pictures.KEPT
    assert all(p["name"] == "Scottie S1" for p in again.status()["pictures"])
    (folder / pictures.INDEX).write_text("{ not json")           # a list that can't be read: begun again, and said
    broken = pictures.Remote()
    broken.keep_in(folder)
    assert broken.status()["pictures"] == []


def test_a_chart_is_slid_straight_afterwards_and_while_it_comes(tmp_path: Path) -> None:
    chart = _chart(60)
    wide = chart[:, np.arange(sstv.FAX_WIDTH) * 512 // sstv.FAX_WIDTH]
    r = pictures.Remote()
    r.keep_in(tmp_path)
    turned = np.roll(wide, 300, axis=1)              # as a chart begun by hand comes: its left edge 300 dots across
    _finished(r, 1, "Weather fax", turned)
    _finished(r, 2, "Robot 36", _card(320, 240))
    n = r.status()["pictures"][0]["n"]
    r.slide(n, 300 / sstv.FAX_WIDTH)
    assert (r.picture(n) == wide).all()
    again = pictures.Remote()                        # ...and that's how it's kept
    again.keep_in(tmp_path)
    assert (again.picture(again.status()["pictures"][0]["n"]) == wide).all()
    for bad in ((99, 0.5), (r.status()["pictures"][1]["n"], 0.5), (n, 1.5), (n, "half"), (n, True)):      # no such chart; a colour picture; not a place
        with pytest.raises(ValueError):
            r.slide(*bad)
    # while it's still coming: the reader turns what it has and lays the rest out to match
    f = np.concatenate([1500.0 + chart[k][(np.arange(int(0.5 * RATE)) * 512 // int(0.5 * RATE))] / 255.0 * 800.0 for k in range(60)])
    x = sstv._tone(f, RATE) * 3000
    reader = pictures.Reader("fax")
    reader.on = True
    rows, step = bytearray(), int(RATE * pictures.STEP_S)
    start = None
    for k, i in enumerate(range(0, len(x), step)):
        if k == 2:
            reader.order("begin")
        if k == 12:                                  # ten seconds of it read: now it's told where its lines begin
            pic = np.frombuffer(bytes(rows), np.uint8).reshape(-1, sstv.FAX_WIDTH)
            edge = int(np.argmax(np.mean(pic[2:], axis=0) < 60))        # the chart's black line down its left side is 60 dots in...
            start = ((edge - 60) % sstv.FAX_WIDTH) / sstv.FAX_WIDTH     # ...so a line begins here
            reader.order(f"slide:{start:.5f}")
        reader.take(x[i:i + step])
        reader.step()
        for item in reader.news():
            if "rows" in item:
                del rows[item["from"] * sstv.FAX_WIDTH:]
                rows += item["rows"]
    pic = np.frombuffer(bytes(rows), np.uint8).reshape(-1, sstv.FAX_WIDTH)
    assert start is not None and len(pic) > 40
    for row in (3, len(pic) - 3):                    # the lines read before it was slid, and those after: the same way round, the chart's
        assert abs(int(np.argmax(pic[row] < 60)) - 60) <= 4


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
        code, data = call(f"/api/rx/pictures/{p['n']}.png?clean=1", raw=True)            # with the speckle taken out: the same picture, near enough
        assert code == 200 and _png(data)[:3] == (320, 240, 2) and _off(pictures.unpng(data), img) < 12
        assert p["grey"] is False and p["kept"] is False                                 # (nowhere to keep it, in this test)
        for bad in ({"slide": 0.5}, {"slide": 0.5, "n": 99}, {"slide": 0.5, "n": p["n"]}, {"slide": 2, "n": p["n"]}):      # not a chart, or no such one
            assert call("/api/rx/pictures", bad)[0] == 400
        assert call("/api/rx/pictures", {"on": True, "mode": "fax"})[1]["pictures"] == "fax"
        assert [q["n"] for q in call("/api/rx/pictures")[1]["pictures"]] == [p["n"]]      # (the other mode: the pictures read stay)
        assert call("/api/rx/pictures", {"begin": True})[0] == 200 and call("/api/rx/pictures", {"end": True})[0] == 200
        assert call("/api/rx/pictures", {"on": False})[1]["pictures"] is None and not pictures.reader.on
    finally:
        pictures.reader.switch(False)
        httpd.shutdown()
        httpd.server_close()
