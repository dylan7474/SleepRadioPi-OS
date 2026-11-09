"""Notifications (playback/notify.py): which alerts may be said, and while the radio is doing what."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sleepradiopi.playback import lightning, notify


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(notify, "modes", {})
    monkeypatch.setattr(notify, "DEFAULT_MODE", "off")


def _notifier(found, paused=False, quiet=False, clock_ok=True):
    said = []
    st = {"paused": paused, "quiet": quiet}
    n = notify.Notifier(found=lambda: found, paused=lambda: st["paused"], quiet=lambda: st["quiet"], clock_ok=lambda: clock_ok,
                        say=lambda words, label: said.append((words, label)), again_s=100.0)
    return n, said, st


AURORA = [("aurora", 1, "AuroraWatch UK has gone to amber.", "Aurora: amber")]


def test_muted_is_the_default_and_says_nothing() -> None:
    n, said, _ = _notifier(AURORA)
    assert n.tick(1000.0) == [] and said == []


def test_the_three_modes_against_the_radio_playing_or_paused() -> None:
    notify.modes["aurora"] = "active"
    n, said, st = _notifier(AURORA, paused=True)
    assert n.tick(1000.0) == [] and said == []                   # armed while playing: not while paused
    st["paused"] = False
    assert n.tick(1010.0) == ["AuroraWatch UK has gone to amber."]       # ...and said once it's playing (it was not marked said)
    assert said == [("AuroraWatch UK has gone to amber.", "Aurora")]
    notify.modes["aurora"] = "always"
    n, said, st = _notifier(AURORA, paused=True)
    assert n.tick(1000.0) == ["AuroraWatch UK has gone to amber."]       # playing or paused: while paused too
    notify.modes["aurora"] = "off"
    n, said, st = _notifier(AURORA)
    assert n.tick(1000.0) == [] and said == []


def test_nothing_gets_past_the_quiet_hours_or_a_wrong_clock() -> None:
    notify.modes["aurora"] = "always"
    n, said, st = _notifier(AURORA, quiet=True)
    assert n.tick(1000.0) == [] and said == []
    st["quiet"] = False
    assert n.tick(1010.0) != []                                  # (held back, not lost)
    n, said, _ = _notifier(AURORA, clock_ok=False)
    assert n.tick(1000.0) == [] and said == []


def test_each_alert_once_again_when_higher_and_after_it_ends() -> None:
    notify.modes["noaa"] = "always"
    cur = [("noaa:G", 3, "NOAA reports a strong geomagnetic storm: G3.", "G3")]
    said = []
    n = notify.Notifier(found=lambda: cur, paused=lambda: False, quiet=lambda: False, clock_ok=lambda: True,
                        say=lambda w, label: said.append(w), again_s=100.0)
    n.tick(0.0)
    n.tick(10.0)
    assert len(said) == 1                                        # once
    cur[:] = [("noaa:G", 4, "NOAA reports a severe geomagnetic storm: G4.", "G4")]
    n.tick(20.0)
    assert len(said) == 2                                        # higher: again
    cur[:] = []
    n.tick(30.0)
    n.tick(200.0)                                                # ended for longer than again_s: forgotten
    cur[:] = [("noaa:G", 3, "NOAA reports a strong geomagnetic storm: G3.", "G3")]
    n.tick(210.0)
    assert len(said) == 3


def test_several_at_once_are_one_line_and_lightning_is_left_to_the_bell() -> None:
    notify.modes.update({"aurora": "always", "quake": "always", "lightning": "always"})
    found = AURORA + [("quake:x", 1, "A magnitude 6.5 earthquake.", "M6.5"), ("lightning:1", 1, "Lightning within 40 km.", "Lightning")]
    n, said, _ = _notifier(found)
    n.tick(1000.0)
    assert said == [("AuroraWatch UK has gone to amber. A magnitude 6.5 earthquake.", "Alerts")]


def test_a_failure_in_saying_does_not_stop_the_thread() -> None:
    notify.modes["aurora"] = "always"

    def boom(words, label):
        raise RuntimeError("no flite")
    n = notify.Notifier(found=lambda: AURORA, paused=lambda: False, quiet=lambda: False, clock_ok=lambda: True, say=boom)
    n.tick(1000.0)


def test_the_default_follows_the_old_alerts_switch() -> None:
    notify.set_default(True)
    assert notify.mode("aurora") == "active"
    notify.set_default(False)
    assert notify.mode("aurora") == "off"
    assert notify.clean({"aurora": "always", "bogus": "always", "quake": "sometimes"}) == {"aurora": "always"}


def test_near_words() -> None:
    st = {"t": 1000.0, "strikes": [{"t": 990.0, "km": 12, "deg": 45}, {"t": 900.0, "km": 30, "deg": 90}]}
    w = lightning.near_words(st, 1000.0)
    assert "12 kilometres" in w and "north-east" in w and "2 in the last hour" in w
    assert lightning.near_words({"t": 1000.0, "strikes": []}, 1000.0) == ""


def test_the_page_api(tmp_path, monkeypatch) -> None:
    from sleepradiopi.web.server import make_handler
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    speaker = type("S", (), {"play_clip": lambda self, clip: None})()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, speaker, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    said = []
    monkeypatch.setattr(notify, "notifier", type("N", (), {"say": lambda self, w, label: said.append((w, label))})())
    monkeypatch.setattr(lightning, "WHEN", "always")
    def post(body):
        req = urllib.request.Request(base + "/api/notify", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        with urllib.request.urlopen(base + "/api/notify", timeout=5) as r:
            d = json.loads(r.read())
        assert [k["key"] for k in d["kinds"]] == list(notify.KINDS) and d["modes"] == ["off", "active", "always"]
        assert {k["key"]: k["mode"] for k in d["kinds"]}["aurora"] == "off" and {k["key"]: k["mode"] for k in d["kinds"]}["lightning"] == "always"
        for bad in ({}, {"modes": {}}, {"modes": {"aurora": "sometimes"}}, {"modes": {"bogus": "off"}}, {"test": "bogus"}):
            assert post(bad)[0] == 400
        assert post({"modes": {"aurora": "active", "lightning": "off"}})[0] == 200
        saved = json.loads(conf.read_text())
        assert saved["notify"] == {"aurora": "active"} and saved["lightning_when"] == "off" and lightning.WHEN == "off"
        assert post({"test": "aurora"})[0] == 200 and said[0][1] == "Aurora"
    finally:
        httpd.shutdown()
        httpd.server_close()
