import json
import threading
import time
from pathlib import Path

import pytest

from sleepradiopi import updater as up
from sleepradiopi.config.reset import KEEP, factory_reset
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.io import service
from sleepradiopi.io.knob import Chord, PressTimer
from sleepradiopi.io.announce import spoken_ip


class Rig:
    """A menu with its words and actions recorded."""

    def __init__(self, wait_s=30.0, rollback_ok=True):
        self.said, self.done = [], []
        acts = {name: (lambda words, name=name: (self.said.append(words), self.done.append(name)))
                for name in ("restart", "reset")}
        acts["wifi"] = lambda: self.done.append("wifi")
        acts["status"] = lambda: "Status report."
        acts["rollback"] = lambda words: (self.said.append(words), self.done.append("rollback"))[0] or rollback_ok
        self.menu = service.ServiceMenu(self.said.append, acts, wait_s=wait_s)

    def press(self, *buttons):
        for n in buttons:
            self.menu.press(n - 1)
            worker = getattr(self.menu, "worker", None)
            if worker is not None:
                worker.join(2)


def test_one_restarts_and_two_resets_the_wifi() -> None:
    r = Rig()
    r.menu.open()
    assert r.menu.active and r.said[-1] == service.MENU
    r.press(1)
    assert r.done == ["restart"] and not r.menu.active and r.said[-1] == service.RESTARTING
    r.menu.open()
    r.press(2)
    assert r.done[-1] == "wifi" and r.said[-1] == service.WIFI


def test_three_is_a_status_report_then_three_again_goes_back() -> None:
    r = Rig()
    r.menu.open()
    r.press(3)
    assert r.said[-1] == f"Status report. {service.ROLLBACK_ASK}" and r.menu.state == "rollback?"
    r.press(3)
    assert r.done == ["rollback"] and r.said[-1] == service.ROLLBACK and not r.menu.active
    r.menu.open()
    r.press(3, 1)                                    # anything else: cancelled
    assert r.done == ["rollback"] and r.said[-1] == service.CANCELLED


def test_going_back_with_nothing_to_go_back_to() -> None:
    r = Rig(rollback_ok=False)
    r.menu.open()
    r.press(3, 3)
    assert r.said[-2:] == [service.ROLLBACK, service.NO_ROLLBACK]


def test_four_is_a_factory_reset_confirmed_by_two_then_three() -> None:
    r = Rig()
    r.menu.open()
    r.press(4)
    assert r.said[-1] == service.RESET_ASK
    r.press(2)
    assert r.said[-1] == service.RESET_NEXT and not r.done
    r.press(3)
    assert r.done == ["reset"] and r.said[-1] == service.RESETTING
    for wrong in ((4, 3), (4, 2, 2), (4, 1)):
        r.menu.open()
        r.press(*wrong)
        assert r.done == ["reset"] and r.said[-1] == service.CANCELLED and not r.menu.active


def test_doing_nothing_closes_it() -> None:
    r = Rig(wait_s=0.2)
    r.menu.open()
    time.sleep(0.5)
    assert not r.menu.active and r.said[-1] == service.CLOSED


def test_while_it_is_open_the_buttons_belong_to_it() -> None:
    class Station:
        _has_voice = False
        source = None
    p = presets_mod.Presets(Station(), None, None, [{"kind": "action", "action": "time"}, None, None, None])
    r = Rig()
    p.menu = r.menu
    r.menu.open()
    p.hold(0)                                        # a hold doesn't keep a preset
    assert r.done == ["restart"] and p.presets[0] == {"kind": "action", "action": "time"}
    assert not r.menu.active


def test_holding_one_and_four_opens_it_and_neither_button_acts() -> None:
    pressed, fired, started = [], [], []
    timers = {c: PressTimer(lambda c=c: pressed.append(c), lambda c=c: pressed.append(("hold", c)), long_s=0.3)
              for c in (2, 3, 4, 5)}
    ticks = []
    chord = Chord({2, 5}, 0.4, on_start=lambda: started.append(1), on_fire=lambda: fired.append(1),
                  on_tick=lambda: ticks.append(1), tick_s=0.1)

    def down(c):
        timers[c].down(); chord.key(c, True, timers)

    def up(c):
        chord.key(c, False, timers); timers[c].up()
    down(2); down(5)
    assert started == [1]
    time.sleep(0.6)
    up(2); up(5)
    assert fired == [1] and pressed == []            # no press, no hold (which would store a preset)
    assert 3 <= len(ticks) <= 4                      # counting, out loud
    down(2); down(5); time.sleep(0.1); up(5); up(2)  # let go early
    time.sleep(0.5)
    assert fired == [1] and pressed == []
    down(3); up(3)                                   # a button on its own still works
    assert pressed == [3]


def test_status_words() -> None:
    text = service.status_text([("wlan0", "192.168.50.130")], {"mode": "station", "ssid": "CHIGLEY"}, "1.0.7",
                               12.4, 2211, spoken_ip)
    assert "Wi-Fi network CHIGLEY" in text and "one nine two, dot" in text and "version 1.0.7" in text
    assert "2211 songs, and 12 gigabytes free" in text
    spot = service.status_text([], {"mode": "hotspot", "hotspot": {"ssid": "SleepRadio-Setup"}}, "1", None, 0, spoken_ip)
    assert "own network, SleepRadio-Setup" in spot and "not connected" in spot


def test_factory_reset_keeps_the_radio_and_forgets_the_rest(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music", "speaker_enabled": True, "speaker_eq": {"bass": 3},
                                "broadcast_voice": "personal", "web_password": "pbkdf2$...", "birthdays": [{}],
                                "buttons": [None], "messages": {"list": []}, "stream_source": {"kind": "radio"}}))
    wifi, volume = tmp_path / "wifi.json", tmp_path / "speaker.json"
    wifi.write_text("{}"); volume.write_text("{}")
    kept = factory_reset(conf, wifi, volume)
    assert json.loads(conf.read_text()) == {"music_folder": "/media/music", "speaker_enabled": True,
                                            "speaker_eq": {"bass": 3}, "broadcast_voice": "personal"}
    assert set(kept) <= KEEP and not wifi.exists() and not volume.exists()


@pytest.fixture
def slots(tmp_path: Path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "cmdline.txt").write_text("console=serial0 root=/dev/mmcblk0p3 rootfstype=squashfs ro\n")
    (tmp_path / "proc_cmdline").write_text("console=serial0 root=/dev/mmcblk0p3 rootfstype=squashfs ro")
    (tmp_path / "slot_p2").write_bytes(b"hsqs" + b"\0" * 60)
    (tmp_path / "slot_p3").write_bytes(b"hsqs" + b"\0" * 60)
    return tmp_path, up.Paths(boot=boot, proc_cmdline=tmp_path / "proc_cmdline", state=tmp_path / "state",
                              device=lambda s: tmp_path / f"slot_{s}", remount=lambda mode: None,
                              rollback_status=tmp_path / "run" / "rollback-status")


def test_going_back_to_the_previous_version(slots) -> None:
    tmp, p = slots
    rebooted = []
    assert up.rollback(p, reboot=lambda: rebooted.append(1)) == "back to p2"
    assert "root=/dev/mmcblk0p2" in (p.boot / "cmdline.txt").read_text() and rebooted == [1]
    assert json.loads((p.state / "result.json").read_text())["rolled_back"]
    assert json.loads(p.rollback_status.read_text()) == {"ok": True, "slot": "p2"}
    said = []
    ups = up.Updates(say=said.append, state=p.state, run=tmp / "run", version_file=tmp / "none")
    ups.on_air()
    assert said and "gone back to its previous version" in said[0]


def test_no_previous_version(slots) -> None:
    tmp, p = slots
    (tmp / "slot_p2").write_bytes(b"\xff" * 64)      # a blank slot (a fresh card)
    rebooted = []
    assert up.rollback(p, reboot=lambda: rebooted.append(1)) == "nothing to go back to"
    assert "root=/dev/mmcblk0p3" in (p.boot / "cmdline.txt").read_text() and not rebooted
    assert json.loads(p.rollback_status.read_text())["ok"] is False


def test_the_page_buttons_go_down_and_up_through_the_real_timers(tmp_path: Path, monkeypatch) -> None:
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from sleepradiopi.web import server as server_mod
    from test_artist_radio import _station
    st = _station(tmp_path)
    p = presets_mod.Presets(st, None, None, [])
    events = []
    p.keys = {c: (lambda c=c: events.append(("down", c)), lambda c=c: events.append(("up", c))) for c in (2, 3, 4, 5)}
    monkeypatch.setattr(server_mod, "KEY_HELD_MAX_S", 0.3)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler(st, None, presets=p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def key(body):
        req = urllib.request.Request(base + "/api/buttons/key", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert key({"button": 1, "down": True}) == 200
        assert key({"button": 1, "down": True}) == 200              # (a repeat: not a second press)
        assert key({"button": 1, "down": False}) == 200
        assert events == [("down", 2), ("up", 2)]
        assert key({"button": 4, "down": True}) == 200
        time.sleep(0.6)                                              # the page never let go
        assert events[-2:] == [("down", 5), ("up", 5)]
        assert key({"button": 4, "down": False}) == 200 and len(events) == 4
        assert key({"button": 5, "down": True}) == 400 and key({"button": 1}) == 400
    finally:
        httpd.shutdown()


def test_the_status_is_made_ahead_and_the_wait_starts_after_the_words() -> None:
    made, said = [], []
    busy = [True]
    acts = {"status": lambda: "Status report.", "restart": lambda: None, "wifi": lambda: None,
            "rollback": lambda: True, "reset": lambda: None}
    m = service.ServiceMenu(said.append, acts, prepare=made.append, wait_s=0.3, busy=lambda: busy[0])
    m.holding()
    time.sleep(0.2)
    assert made == [service.MENU, service.RESTARTING, f"Status report. {service.ROLLBACK_ASK}"]
    m.open()
    time.sleep(0.8)
    assert m.active                                  # still talking: not closed
    busy[0] = False
    time.sleep(0.6)
    assert not m.active and said[-1] == service.CLOSED


def test_the_menu_pauses_the_show_and_resumes_it_after() -> None:
    events = []
    r = Rig()
    r.menu.on_open = lambda: events.append("pause")
    r.menu.on_close = lambda resume: events.append(("close", resume))
    r.menu.open()
    r.menu.open()                                    # (already open: not paused twice)
    assert events == ["pause"]
    r.press(4, 1)                                    # cancelled: back to the show
    assert events == ["pause", ("close", True)] and r.said[-1] == service.CANCELLED
    r.menu.open()
    r.press(1)                                       # a restart: stays quiet
    assert events[-1] == ("close", False)


def test_the_menu_sound_ticks_while_words_are_made_then_says_them() -> None:
    import numpy as np
    tick = np.full((10, 2), 1000, np.int16)
    s = service.MenuSound(tick, rate=100, channels=2, tick_s=1.0)
    assert not s.next(100).any() and not s.talking             # nothing to say: silence
    s.making = 1
    out = s.next(200)
    assert out[:10].all() and not out[10:100].any() and out[100:110].all()   # a tick a second
    s.making = 0
    s.put(np.full((50, 2), 7, np.int16))
    assert s.talking
    out = s.next(80)
    assert (out[:50] == 7).all() and not out[50:].any() and not s.talking
    s.put(np.full((50, 2), 1, np.int16))
    s.put(np.full((30, 2), 2, np.int16))                       # a new line cuts the old one off
    assert (s.next(30) == 2).all()
