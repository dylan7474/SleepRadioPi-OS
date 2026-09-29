import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.io.lamps import REF_MEAN_ABS, Lamps, Pwm
from sleepradiopi.web.server import make_handler


def _chip(tmp_path: Path) -> Path:
    for ch in (0, 1):
        d = tmp_path / f"pwm{ch}"
        d.mkdir()
        for f in ("period", "enable", "duty_cycle"):
            (d / f).write_text("0")
    return tmp_path


def _duty(root: Path, ch: int) -> int:
    return int((root / f"pwm{ch}" / "duty_cycle").read_text())


class Spk:
    level = 0.0
    level_at = 0.0
    fade_factor = 1.0


class Ctl:
    paused = False


def _lamps(tmp_path, **kw):
    root = _chip(tmp_path)
    spk, ctl = Spk(), Ctl()
    bank = {"now": "day"}
    lamps = Lamps(spk, ctl, bank=lambda: bank["now"], needle=Pwm(0, root, 1000), glow=Pwm(1, root, 1000), **kw)
    return lamps, spk, ctl, bank, root


def test_pwm_missing_is_quietly_off(tmp_path: Path) -> None:
    p = Pwm(0, tmp_path / "nothing")
    assert not p.ok
    p.set(0.5)                     # no error


def test_pwm_enables_and_sets_duty(tmp_path: Path) -> None:
    root = _chip(tmp_path)
    p = Pwm(0, root, 1000)
    assert p.ok and (root / "pwm0" / "enable").read_text() == "1"
    assert (root / "pwm0" / "period").read_text() == "1000"
    p.set(0.25)
    assert _duty(root, 0) == 250
    p.set(2)
    assert _duty(root, 0) == 1000


def test_needle_sits_at_0_vu_for_levelled_music(tmp_path: Path) -> None:
    lamps, *_ = _lamps(tmp_path)
    assert lamps.needle_target(REF_MEAN_ABS) == pytest.approx(10 ** (-3 / 20))
    assert lamps.needle_target(0) == 0
    assert lamps.needle_target(REF_MEAN_ABS * 100) == 1.0
    lamps.set(meter_trim_db=3)
    assert lamps.needle_target(REF_MEAN_ABS) == pytest.approx(1.0)


def test_needle_follows_the_level_and_falls_when_nothing_plays(tmp_path: Path) -> None:
    lamps, spk, _, _, root = _lamps(tmp_path)
    spk.level, spk.level_at = REF_MEAN_ABS, 100.0
    for i in range(20):
        lamps.step(100.0 + i * 0.001, 0.02)
    assert _duty(root, 0) == round(1000 * 10 ** (-3 / 20))
    for i in range(20):
        lamps.step(101.0 + i * 0.02, 0.02)          # the level is over 0.2 s old
    assert _duty(root, 0) == 0


def test_sweep_goes_up_and_back(tmp_path: Path) -> None:
    lamps, _, _, _, root = _lamps(tmp_path)
    lamps._sweep_until = 10.0                       # a 4 s sweep ending at t=10
    lamps.step(8.0, 0.02)
    assert _duty(root, 0) == 1000                   # the top, half way
    lamps.step(9.5, 0.02)
    assert _duty(root, 0) == 250


def test_glow_day_night_pause_and_fade(tmp_path: Path) -> None:
    lamps, spk, ctl, bank, root = _lamps(tmp_path, glow_day=100, glow_night=50)
    for _ in range(100):
        lamps.step(0, 0.02)                          # 2 s: past the 1.5 s ramp
    assert _duty(root, 1) == 1000
    bank["now"] = "night"
    for _ in range(100):
        lamps.step(0, 0.02)
    assert _duty(root, 1) == round(1000 * 0.5 ** 2.2)
    spk.fade_factor = 0.0                            # the sleep timer's fade
    lamps.step(0, 0.02)
    assert 0 < _duty(root, 1) < round(1000 * 0.5 ** 2.2)   # ramps, not a jump
    for _ in range(100):
        lamps.step(0, 0.02)
    assert _duty(root, 1) == 0
    spk.fade_factor = 1.0
    ctl.paused = True
    assert lamps.glow_target() == 0


def test_set_checks_its_values(tmp_path: Path) -> None:
    lamps, *_ = _lamps(tmp_path)
    assert lamps.set(glow_day=30)["glow_day"] == 30
    for bad in ({"glow_day": 101}, {"glow_night": -1}, {"meter_trim_db": 13}, {"glow_day": True}, {"glow_day": "5"}):
        with pytest.raises(ValueError):
            lamps.set(**bad)


class FakeStation:
    def status(self):
        return {"on_air": False}


def test_web_api_saves_and_sweeps(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    (tmp_path / "chip").mkdir()
    lamps, *_ = _lamps(tmp_path / "chip")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, config_file=conf, lamps=lamps))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/lamps") as r:
            d = json.load(r)
        assert d["glow_day"] == 60 and d["needle"] and d["glow"]
        req = urllib.request.Request(base + "/api/lamps", data=json.dumps({"glow_night": 5, "meter_trim_db": -2}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["glow_night"] == 5
        saved = json.loads(conf.read_text())
        assert saved == {"glow_night": 5, "meter_trim_db": -2.0}
        req = urllib.request.Request(base + "/api/lamps/sweep", data=b"{}", method="POST")
        with urllib.request.urlopen(req):
            pass
        assert lamps._sweep_until > 0
    finally:
        httpd.shutdown()


def test_web_api_without_lamps(tmp_path: Path) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/lamps") as r:
            assert json.load(r) == {"needle": False, "glow": False}
    finally:
        httpd.shutdown()
