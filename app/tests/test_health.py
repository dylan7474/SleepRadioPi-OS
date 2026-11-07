"""Is something wrong? The light in the menu bar and /api/health (health.py)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi import health
from sleepradiopi.web.server import make_handler

NOW = 1_800_000_000.0


class _Feed:
    EVERY_S = 900.0

    def __init__(self, name: str, age: float | None, wanted: bool = True) -> None:
        self.NAME, self._age, self._wanted = name, age, wanted

    def age(self):
        return self._age

    def wanted(self):
        return self._wanted


def _health(clock, mem=lambda: 200.0, temp=lambda: 45.0, volt=lambda: False, ooms=lambda: 0, uptime=lambda: 30.0,
            space=lambda p: (160.0, 30.0), appliance=True) -> health.Health:
    return health.Health(Path("/x"), clock, mem, temp, volt, ooms, uptime, space, appliance)


def _keys(h: health.Health, **kw) -> dict:
    out = h.issues(**kw)
    return {i["key"]: i["level"] for i in out["issues"]} | {"_": out["level"]}


def test_a_healthy_radio_says_nothing() -> None:
    clock = [NOW]
    h = _health(lambda: clock[0])
    h.sample()
    assert h.issues([_Feed("NOAA's figures", 100.0)]) == {"level": "ok", "issues": [], "t": NOW}


def test_the_card_filling_up() -> None:
    clock = [NOW]
    h = _health(lambda: clock[0], space=lambda p: (35.0, 70.0))
    assert _keys(h) == {"disk": "warn", "_": "warn"}
    assert _keys(_health(lambda: clock[0], space=lambda p: (100.0, 88.0))) == {"disk": "warn", "_": "warn"}
    bad = _health(lambda: clock[0], space=lambda p: (9.0, 96.0))
    out = bad.issues()
    assert out["level"] == "bad" and "9 MB" in out["issues"][0]["short"]


def test_memory_heat_and_oom_come_from_the_last_five_minutes_of_readings() -> None:
    clock = [NOW]
    mem, temp = [200.0], [45.0]
    h = _health(lambda: clock[0], mem=lambda: mem[0], temp=lambda: temp[0], ooms=lambda: 2)
    h.sample()
    mem[0], temp[0] = 12.0, 77.0                       # a dip that has gone again by the time anyone looks
    clock[0] += 10
    h.sample()
    mem[0], temp[0] = 200.0, 50.0
    clock[0] += 10
    h.sample()
    k = _keys(h)
    assert k["memory"] == "bad" and k["heat"] == "warn" and k["oom"] == "warn" and k["_"] == "bad"
    clock[0] += health.WINDOW_S + 30                   # five minutes on, and nothing new: the dip is forgotten (the kills since power-on are not)
    h.sample()
    assert _keys(h) == {"oom": "warn", "_": "warn"}


def test_a_power_dip_is_shown_for_a_day_and_red_while_it_lasts() -> None:
    clock = [NOW]
    volt = [True]
    h = _health(lambda: clock[0], volt=lambda: volt[0])
    h.sample()
    assert _keys(h)["power"] == "bad"
    volt[0] = False
    clock[0] += 600
    h.sample()
    out = h.issues()
    assert out["issues"][0]["level"] == "warn" and "10 min ago" in out["issues"][0]["short"]
    clock[0] += health.UNDERVOLT_SHOWN_S
    h.sample()
    assert h.issues()["issues"] == []


def test_the_program_restarting_itself_is_told_but_not_a_boot_nor_a_desktop() -> None:
    clock = [NOW]
    up = [90_000.0]                                    # the system has been up a day: the program was started again on its own
    h = _health(lambda: clock[0], uptime=lambda: up[0])
    clock[0] += 720
    up[0] += 720
    out = h.issues()
    assert [i["key"] for i in out["issues"]] == ["restart"] and "12 min" in out["issues"][0]["short"]
    clock[0] += health.RESTART_SHOWN_S
    assert h.issues()["issues"] == []                   # (an hour on: no longer news)
    boot = _health(lambda: clock[0], uptime=lambda: 25.0)         # the radio just switched on: the program starts soon after
    assert boot.issues()["issues"] == []
    desktop = _health(lambda: clock[0], uptime=lambda: 90_000.0, appliance=False)
    assert desktop.issues()["issues"] == []


def test_the_clock() -> None:
    h = _health(lambda: NOW)
    assert _keys(h, clock_ok=False) == {"clock": "warn", "_": "warn"}


def test_feeds_that_stopped_updating() -> None:
    clock = [NOW]
    h = _health(lambda: clock[0])
    old = [_Feed("a", 7200.0), _Feed("b", 7200.0), _Feed("c", 7200.0)]
    assert h.issues(old)["issues"] == []                                       # (just started: what they kept in files is old, the first fetch is under way)
    clock[0] += health.FEEDS_GRACE_S + 1
    fresh = _Feed("NOAA's figures", 300.0)
    one = _Feed("the earthquakes", 3 * 3600.0)
    out = h.issues([fresh, one])
    assert [i["key"] for i in out["issues"]] == ["feed:the earthquakes"] and "3 h ago" in out["issues"][0]["says"]
    three = [_Feed("the earthquakes", 3 * 3600.0), _Feed("AuroraWatch UK's alert", 2 * 3600.0), _Feed("N0NBH's forecast", 6 * 3600.0), fresh]
    out = h.issues(three)
    assert [i["key"] for i in out["issues"]] == ["feeds"] and "3 outside feeds" in out["issues"][0]["says"] and "6 h" in out["issues"][0]["says"]
    assert h.issues(three, offline=True)["issues"] == []                       # a radio set to work without Wi-Fi expects none
    assert h.issues([_Feed("PSK Reporter's spots", None, wanted=False)])["issues"] == []   # (no callsign set: not asked for)
    never = [_Feed("a", None), _Feed("b", None), _Feed("c", None)]
    assert h.issues(never)["issues"] == []                                     # (a few minutes in: they haven't had time)
    clock[0] += health.FEEDS_QUIET_S + 1
    assert [i["key"] for i in h.issues(never)["issues"]] == ["feeds"]


def test_health_from_the_web(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/health", timeout=5) as r:
            d = json.loads(r.read())
        assert d["level"] in ("ok", "warn", "bad") and isinstance(d["issues"], list) and "t" in d
    finally:
        httpd.shutdown()
        httpd.server_close()
