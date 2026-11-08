"""433 MHz devices from the receiver's background service on the local network (playback/radio433.py)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi import health as health_mod
from sleepradiopi.playback import conditions, radio433
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0
SCAN = {"running": True, "since": NOW - 5000, "messages": 120, "devices": [
    {"id": "aa51b898", "model": "Ambientweather-F007TH", "count": 80, "first": NOW - 4000, "last": NOW - 30, "ago_s": 30, "rssi": -12.1, "snr": 22.2, "freq": 433.91},
    {"id": "0badf00d", "model": "Acurite-Tower", "count": 30, "ago_s": 40, "rssi": -9.0, "snr": 18.0, "name": "Shed", "readings": {"temperature_C": 7.5, "humidity": 81, "battery_ok": 1}},
    {"id": "c0ffee00", "model": "Nexus-TH", "count": 10, "ago_s": 7200, "snr": 5.0},
    {"nonsense": True}, "garbage"]}
STATUS = {"job": "scan433", "receiver_busy": False, "held_s": 0, "sonde_windows_utc": ["22:45-01:30", "10:45-13:30"], "sonde_heard_ago_s": None}


def _get(answers: dict):
    def get(url, timeout=6.0):
        for k, v in answers.items():
            if url.endswith(k):
                if v is None:
                    raise urllib.error.URLError("no route")
                return v
        raise urllib.error.URLError("unknown " + url)
    return get


def _r(answers=None, address="192.0.2.10:8076", clock=None):
    t = [NOW]
    r = radio433.Radio433(get=_get(answers or {"/433": SCAN, "/status": STATUS}), clock=clock or (lambda: t[0]), address=lambda: address)
    r.t = t
    return r


def test_it_is_asked_only_with_an_address_and_goes_when_the_receiver_does() -> None:
    assert _r(address="").status() is None and not _r(address="").wanted()
    r = _r()
    assert r.wanted() and r.LAN and r.fetch() is not None
    had = r.status(False)
    assert had["job"] == "scan433" and had["windows"] == ["22:45-01:30", "10:45-13:30"] and had["messages"] == 120
    assert [d["model"] for d in had["devices"]] == ["Ambientweather-F007TH", "Acurite-Tower", "Nexus-TH"]        # (nonsense dropped, the latest heard first)
    r.t[0] += 60
    assert r.status(False)["devices"][0]["ago_s"] == 90                  # (heard 30 s before the look, and the look was a minute ago)
    r.t[0] += radio433.OLD_S
    assert r.status(False) is None                                       # (an old picture of the air is no picture)
    down = _r({"/433": None, "/status": None})
    assert down.fetch() is None and down.status(False) is None


def test_only_your_own_devices_show_readings_and_ids_are_the_receivers() -> None:
    r = _r()
    r.fetch()
    devs = {d["model"]: d for d in r.status(False)["devices"]}
    assert devs["Acurite-Tower"]["name"] == "Shed" and devs["Acurite-Tower"]["readings"] == {"temperature_C": 7.5, "humidity": 81, "battery_ok": 1}
    assert "readings" not in devs["Ambientweather-F007TH"] and "name" not in devs["Ambientweather-F007TH"]
    assert radio433.reading(devs["Acurite-Tower"]) == "7.5°C · 81%" and radio433.reading(devs["Nexus-TH"]) == ""


def test_the_history_is_in_memory_and_counts_only_what_was_seen_to_arrive() -> None:
    r = _r()
    r.fetch()                                                            # (the first look: 120 messages already, none of them counted as new)
    assert sum(n for _, n in r.status(False)["activity"]) == 0
    r.t[0] += 301
    r._get = _get({"/433": {**SCAN, "messages": 150}, "/status": STATUS})
    r.fetch()
    act = r.status(False)["activity"]
    assert sum(n for _, n in act) == 30 and len(act) == 96 and act[-1][0] <= r.t[0] < act[-1][0] + radio433.BUCKET_S
    r.t[0] += 301
    r._get = _get({"/433": {**SCAN, "messages": 5}, "/status": STATUS})   # (the service restarted: no negative counts)
    r.fetch()
    assert sum(n for _, n in r.status(False)["activity"]) == 30
    series = r.status(False)["series"]["Shed"]
    assert len(series) == 3 and series[0][1:] == [7.5, 81] and set(r.status(False)["series"]) == {"Shed"}       # (one every five minutes, named devices only)
    r.forget()
    assert r.status(False) is None
    r.fetch()
    assert r.status(False)["series"] == {"Shed": [[round(r.t[0]), 7.5, 81]]} and sum(n for _, n in r.status(False)["activity"]) == 0


def test_it_says_what_the_dongle_is_doing_in_words() -> None:
    assert radio433.state_word({"job": "scan433"}) == "Scanning"
    assert radio433.state_word({"job": "sondes"}) == "Paused: listening for radiosondes"
    assert radio433.state_word({"job": None, "receiver_busy": True}) == "Paused: the VHF receiver is in use"
    assert radio433.state_word({"job": None, "receiver_busy": False}) == "Starting again"


def test_the_tile() -> None:
    assert radio433.tile(None, set()) == []
    r = _r()
    r.fetch()
    t = radio433.tile(r.status(False), set())[0]
    assert t["key"] == "r433" and t["title"] == "433 MHz" and t["value"] == "2" and t["state"] == "good"       # (the Nexus was heard two hours ago)
    assert t["sub"] == "Shed 7.5°C · 81%" and "Scanning." in t["says"] and "Shed: 7.5°C · 81%." in t["says"]
    busy = _r({"/433": SCAN, "/status": {**STATUS, "job": None, "receiver_busy": True}})
    busy.fetch()
    assert radio433.tile(busy.status(False), set())[0]["sub"].startswith("paused: the VHF receiver is in use")
    quiet = _r({"/433": {**SCAN, "devices": []}, "/status": STATUS})
    quiet.fetch()
    q = radio433.tile(quiet.status(False), set())[0]
    assert q["state"] == "quiet" and q["value"] == "0" and q["sub"] == "nothing heard yet"


def test_it_is_in_the_keeper_and_in_the_world_outside() -> None:
    assert radio433.radio433 in conditions.outside_feeds()
    r = _r()
    r.fetch()
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, r433_now=r.status(False))
    assert "r433" in [t["key"] for t in d["tiles"]]
    assert "r433" not in [t["key"] for t in conditions.outside(conditions.summary({}, [], NOW), None, None, None, True)["tiles"]]


class _Plain:
    NAME = "plain"

    def wanted(self): return True
    def age(self): return None


def test_it_is_not_a_reason_for_the_light() -> None:
    a = radio433.Radio433(get=_get({}), clock=lambda: NOW, address=lambda: "pi:8076")
    clock = [NOW]
    h = health_mod.Health(Path("/x"), lambda: clock[0], lambda: 200.0, lambda: 45.0, lambda: False, lambda: 0, lambda: 30.0, lambda p: (160.0, 30.0), True)
    h.sample()
    clock[0] += 3600
    h.sample()
    assert h.issues([a])["issues"] == []


def test_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    answers = {"/433": SCAN, "/status": STATUS}
    r = radio433.Radio433(get=_get(answers), address=lambda: radio433.ADDRESS)
    monkeypatch.setattr(radio433, "radio433", r)
    monkeypatch.setattr(radio433, "ADDRESS", "")

    def post(body):
        req = urllib.request.Request(base + "/api/radio433", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return json.loads(resp.read())
    try:
        d = get("/api/radio433")
        assert d["address"] == "" and d["up"] is False and d["devices"] == []
        assert "r433" not in [t["key"] for t in get("/api/outside")["tiles"]]
        for bad in ({}, {"address": 3}, {"address": "ftp://x"}, {"address": "a b"}):
            assert post(bad)[0] == 400
        code, d = post({"address": "pi.local:8076"})
        assert code == 200 and d["up"] is True and d["now"] == "Scanning" and d["paused"] is False and len(d["devices"]) == 3
        assert json.loads(conf.read_text())["background_url"] == "pi.local:8076"
        assert "r433" in [t["key"] for t in get("/api/outside")["tiles"]]
        answers["/433"] = None                                                                        # the receiver goes
        code, d = post({"address": "pi.local:8076"})
        assert d["up"] is False and d["address"] == "pi.local:8076"
        assert post({"address": ""})[1]["address"] == "" and json.loads(conf.read_text())["background_url"] == ""
    finally:
        httpd.shutdown()
        httpd.server_close()
