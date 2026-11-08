"""Aircraft overhead from a dump1090 / readsb receiver on the local network (playback/aircraft.py)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi import health as health_mod
from sleepradiopi.audio import ft8
from sleepradiopi.playback import aircraft, conditions
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0
RECEIVER = {"version": "4.0", "refresh": 1000, "history": 120, "lat": 54.5316, "lon": -1.08804}
SKY = {"now": NOW, "messages": 99, "aircraft": [
    {"hex": "48e8f9", "mlat": [], "messages": 6, "seen": 0.8, "rssi": -17.9},                                  # (an address and nothing else)
    {"hex": "440096", "flight": "EJU56BH ", "alt_baro": 24250, "gs": 401.6, "track": 142.6, "baro_rate": 1408, "lat": 55.364, "lon": -2.545},
    {"hex": "400001", "flight": "GNJWI   ", "alt_baro": 725, "gs": 99.0, "track": 10, "baro_rate": -64, "lat": 54.59, "lon": -1.06},
    {"hex": "400002", "flight": "RYR6HD", "alt_baro": "ground", "gs": 3, "lat": 54.52, "lon": -1.09},
    {"hex": "400003", "flight": "NOPOS1", "alt_baro": 9000},                                                    # (a callsign, no position yet)
    {"hex": "400004", "flight": "EMRG1", "alt_baro": 11000, "squawk": "7700", "lat": 54.9, "lon": -1.5},
]}


def _get(answers: dict, asked: list | None = None):
    def get(url, timeout=6.0):
        if asked is not None:
            asked.append(url)
        for k, v in answers.items():
            if url.endswith(k):
                if v is None:
                    raise urllib.error.URLError("no route")
                return v
        raise urllib.error.URLError("unknown " + url)
    return get


def _air(address="192.0.2.10:8080", answers=None, **kw):
    return aircraft.Aircraft(get=_get(answers or {"/data/aircraft.json": SKY, "/data/receiver.json": RECEIVER}), clock=lambda: NOW, address=lambda: address, **kw)


def test_the_address_is_made_safe() -> None:
    assert aircraft.base("192.168.1.5:8080") == "http://192.168.1.5:8080"
    assert aircraft.base(" http://pi.local:8080/anything?x=1 ") == "http://pi.local:8080"
    assert aircraft.base("https://sky.example") == "https://sky.example"
    for bad in ("", "  ", "ftp://x", "file:///etc/passwd", "a b", "http://", "host:99999", "x" * 300, None):
        assert aircraft.base(bad) is None


def test_what_an_aircraft_says() -> None:
    at = (54.5316, -1.08804)
    assert aircraft.plane({"hex": "48e8f9", "mlat": [], "rssi": -9}, at) is None                                # (nothing to show)
    p = aircraft.plane(SKY["aircraft"][2], at)
    assert (p["call"], p["ft"], p["kt"], p["track"]) == ("GNJWI", 725, 99, 10) and "climb" not in p            # (-64 ft/min: level)
    assert 3.5 < p["nm"] < 4.5 and 0 <= p["deg"] < 90
    assert aircraft.plane(SKY["aircraft"][1], at)["climb"] == "up"
    assert aircraft.plane(SKY["aircraft"][3], at)["ground"] is True and "nm" in aircraft.plane(SKY["aircraft"][3], at)
    assert "nm" not in aircraft.plane(SKY["aircraft"][4], at)
    assert aircraft.plane(SKY["aircraft"][5], at)["squawk"] == "7700"
    assert "squawk" not in aircraft.plane({"hex": "a", "flight": "X", "squawk": "1234"}, at)


def test_fetch_keeps_the_nearest_and_the_emergency() -> None:
    asked: list = []
    a = aircraft.Aircraft(get=_get({"/data/aircraft.json": SKY, "/data/receiver.json": RECEIVER}, asked), clock=lambda: NOW, address=lambda: "192.0.2.10:8080")
    had = a.fetch()
    assert asked == ["http://192.0.2.10:8080/data/aircraft.json", "http://192.0.2.10:8080/data/receiver.json"]
    assert (had["n"], had["placed"], had["aerial"], had["from"]) == (6, 4, True, "receiver")
    assert [p["call"] for p in had["planes"]] == ["RYR6HD", "GNJWI", "EMRG1", "EJU56BH", "NOPOS1"]              # placed by distance, then the rest
    assert [p["call"] for p in had["emergency"]] == ["EMRG1"] and had["far_nm"] > 50
    a.fetch()
    assert asked.count("http://192.0.2.10:8080/data/receiver.json") == 1                                  # (the aerial is asked once)


def test_without_receiver_json_the_radios_own_locator_is_used(monkeypatch) -> None:
    monkeypatch.setattr(ft8, "HOME", "IO94")
    had = _air(answers={"/data/aircraft.json": SKY, "/data/receiver.json": None}).fetch()
    assert had["aerial"] is True and had["from"] == "locator" and had["placed"] == 4
    monkeypatch.setattr(ft8, "HOME", None)
    had = _air(answers={"/data/aircraft.json": SKY, "/data/receiver.json": None}).fetch()
    assert had["aerial"] is False and had["from"] is None and had["placed"] == 0 and had["n"] == 6 and had["far_nm"] is None


def test_it_goes_when_the_receiver_does() -> None:
    now = [NOW]
    answers = {"/data/aircraft.json": SKY, "/data/receiver.json": RECEIVER}
    a = aircraft.Aircraft(get=_get(answers), clock=lambda: now[0], address=lambda: "pi:8080")
    assert a.fetch() and a.status(False)["n"] == 6
    answers["/data/aircraft.json"] = None
    assert a.fetch() is None and a.status(False)["n"] == 6                                                    # (it didn't answer: the last picture, for now)
    now[0] += aircraft.OLD_S + 1
    assert a.status(False) is None and a.status(False, True) is None                                          # (then no tile: never "old")


def test_no_address_no_asking() -> None:
    asked: list = []
    address = [""]
    a = aircraft.Aircraft(get=_get({"/data/aircraft.json": SKY}, asked), clock=lambda: NOW, address=lambda: address[0])
    assert not a.wanted() and a.fetch() is None and a.status() is None
    a.refresh()
    assert asked == []
    address[0] = "pi:8080"
    assert a.wanted() and a.fetch()
    address[0] = ""
    assert a.status(False) is None                                                                            # (forgotten: gone at once)


class _Plain:
    EVERY_S, NAME = 900.0, "some figures"

    def age(self):
        return None

    def wanted(self):
        return True


def test_it_is_not_a_reason_for_the_light() -> None:
    a = aircraft.Aircraft(get=_get({}), clock=lambda: NOW, address=lambda: "pi:8080")                          # (set, and never answering)
    assert a.wanted() and a.age() is None and a.LAN
    clock = [NOW]
    h = health_mod.Health(Path("/x"), lambda: clock[0], lambda: 200.0, lambda: 45.0, lambda: False, lambda: 0, lambda: 30.0, lambda p: (160.0, 30.0), True)
    h.sample()
    clock[0] += 3600                                                                                          # (long past the grace; an ordinary feed would be flagged)
    h.sample()
    assert h.issues([_Plain()])["issues"][0]["key"].startswith("feed:")
    assert h.issues([a])["issues"] == []


def test_tile_and_alert(monkeypatch) -> None:
    had = _air().fetch()
    t = aircraft.tile(had, set())[0]
    assert (t["key"], t["value"], t["state"]) == ("aircraft", "6", "good")
    assert t["sub"] == "EMRG1 squawking 7700" and "below 3,000 ft" in t["says"]                                  # (an alert says what it is)
    calm = aircraft.tile({**had, "emergency": []}, set())[0]
    assert calm["sub"].startswith("RYR6HD · ") and calm["sub"].endswith(" nm S")
    assert aircraft.tile(None, set()) == [] and aircraft.tile({"n": 0, "placed": 0, "planes": [], "emergency": []}, set())[0]["state"] == "quiet"
    found = aircraft.alerts(had)
    assert found[0][0] == "aircraft:400004:7700" and "EMRG1 is squawking 7700: a general emergency" in found[0][2] and found[0][3] == "EMRG1 squawking 7700"
    assert aircraft.alerts(None) == [] and aircraft.alerts({"emergency": []}) == []
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, air_now=had)
    assert [x["key"] for x in d["tiles"]] == ["qrz", "aircraft"] and d["alerts"][0]["tile"] == "aircraft"
    assert [x for x in d["tiles"] if x["key"] == "aircraft"][0]["state"] == "alert"
    # the DJ says it once, with Alerts on
    class Fake:
        def status(self, *a):
            return had
    monkeypatch.setattr(aircraft, "aircraft", Fake())
    monkeypatch.setattr(conditions, "ALERTS", True)
    monkeypatch.setattr(conditions, "_alerted", {})
    from datetime import datetime
    at = datetime.fromtimestamp(NOW)
    assert "EMRG1 is squawking 7700" in conditions.alert_due(at, True, False, NOW)
    assert conditions.alert_due(at, True, False, NOW + 60) is None


def test_the_feed_is_in_the_keeper() -> None:
    assert aircraft.aircraft in conditions.outside_feeds()


def test_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    answers = {"/data/aircraft.json": SKY, "/data/receiver.json": RECEIVER}
    a = aircraft.Aircraft(get=_get(answers), address=lambda: aircraft.ADDRESS)
    monkeypatch.setattr(aircraft, "aircraft", a)
    monkeypatch.setattr(aircraft, "ADDRESS", "")

    def post(body):
        req = urllib.request.Request(base + "/api/aircraft", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return json.loads(r.read())
    try:
        d = get("/api/aircraft")
        assert d["address"] == "" and d["up"] is False and d["planes"] == []
        assert "aircraft" not in [t["key"] for t in get("/api/outside")["tiles"]]
        for bad in ({}, {"address": 3}, {"address": "ftp://x"}, {"address": "a b"}):
            assert post(bad)[0] == 400
        code, d = post({"address": "pi.local:8080"})
        assert code == 200 and d["up"] is True and d["n"] == 6 and d["emergency"][0]["call"] == "EMRG1"
        assert json.loads(conf.read_text())["aircraft_url"] == "pi.local:8080"
        assert "aircraft" in [t["key"] for t in get("/api/outside")["tiles"]]
        answers["/data/aircraft.json"] = None                                                                 # the receiver goes
        code, d = post({"address": "pi.local:8080"})
        assert d["up"] is False and d["address"] == "pi.local:8080"
        assert post({"address": ""})[1]["address"] == "" and json.loads(conf.read_text())["aircraft_url"] == ""
    finally:
        httpd.shutdown()
        httpd.server_close()


HELI = {"hex": "43c0a1", "flight": "RRR123 ", "alt_baro": 900, "gs": 60, "track": 90, "lat": 54.55, "lon": -1.07}      # (~1 nm away, low, an RAF address)
SKY2 = {**SKY, "aircraft": SKY["aircraft"] + [HELI]}


def test_what_to_watch_for() -> None:
    assert aircraft.parse_watch("") == [] and aircraft.parse_watch(None) == []
    m = aircraft.parse_watch("rrr, ASCOT #43c0a1 #43C000-43CFFF mil")
    assert m[:2] == [("call", "RRR"), ("call", "ASCOT")] and ("hex", 0x43C0A1, 0x43C0A1) in m and ("hex", 0xAE0000, 0xAFFFFF) in m
    for bad in ("R", "#12", "#GGGGGG", "#43CFFF-43C000", "a b!", "x" * 9, "RRR;DROP"):
        try:
            aircraft.parse_watch(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    p = {"hex": "43c0a1", "call": "RRR123"}
    assert aircraft.watched(p, aircraft.parse_watch("RRR")) and aircraft.watched(p, aircraft.parse_watch("#43C0A1")) and aircraft.watched(p, aircraft.parse_watch("mil"))
    assert not aircraft.watched(p, aircraft.parse_watch("ASCOT, #43C0A2")) and not aircraft.watched({"hex": "~12345"}, aircraft.parse_watch("mil"))


def test_low_and_close_and_watched_are_for_the_eye_only() -> None:
    a = aircraft.Aircraft(get=_get({"/data/aircraft.json": SKY2, "/data/receiver.json": RECEIVER}), clock=lambda: NOW, address=lambda: "pi:8080", watch=lambda: "mil")
    had = a.fetch()
    assert [p["call"] for p in had["low"]] == ["RRR123"] and [p["call"] for p in had["watch"]] == ["RRR123"]        # (GNJWI is low but 3.7 nm out)
    seen = aircraft.alerts(had)
    assert [w for w, *_ in seen] == ["aircraft:400004:7700", "aircraft:low:43c0a1", "aircraft:watch:43c0a1"]
    assert seen[1][3].startswith("Low: RRR123 900 ft · ") and seen[2][3].startswith("Watching: RRR123 ")
    assert [w for w, *_ in aircraft.alerts(had, spoken=True)] == ["aircraft:400004:7700"]                        # (the DJ says only the emergency)
    t = aircraft.tile(had, {"aircraft"})[0]
    assert t["state"] == "alert" and t["sub"] == "EMRG1 squawking 7700" and "Low aircraft RRR123" in t["says"]
    calm = {**had, "emergency": []}
    assert aircraft.tile(calm, {"aircraft"})[0]["sub"].startswith("Low: RRR123")
    assert aircraft.tile({**calm, "low": [], "watch": []}, set())[0]["sub"].startswith("RYR6HD · ")
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, air_now=calm)
    assert [x["tile"] for x in d["alerts"]] == ["aircraft", "aircraft"] and [x["key"] for x in d["alerts"]][0] == "aircraft:low:43c0a1"


def test_not_watching_not_alerting() -> None:
    had = _air(answers={"/data/aircraft.json": {**SKY2, "aircraft": [dict(HELI, alt_baro="ground")]}, "/data/receiver.json": RECEIVER}).fetch()
    assert had["low"] == [] and had["watch"] == []                                                                  # (on the ground: not low; nothing watched)


def test_a_deaf_receiver_is_said_once_it_has_been_an_hour() -> None:
    now = [NOW]
    answers = {"/data/aircraft.json": {"aircraft": []}, "/data/receiver.json": RECEIVER}
    a = aircraft.Aircraft(get=_get(answers), clock=lambda: now[0], address=lambda: "pi:8080")
    a.fetch()
    assert a.trouble() is None
    now[0] += aircraft.DEAF_S - 1
    a.fetch()
    assert a.trouble() is None
    now[0] += 2
    a.fetch()
    t = a.trouble()
    assert t["key"] == "aircraft-deaf" and t["level"] == "warn" and "no aircraft for 1 h" in t["text"]
    clock = [NOW]
    h = health_mod.Health(Path("/x"), lambda: clock[0], lambda: 200.0, lambda: 45.0, lambda: False, lambda: 0, lambda: 30.0, lambda p: (160.0, 30.0), True)
    h.sample()
    assert [i["key"] for i in h.issues([a])["issues"]] == ["aircraft-deaf"]
    answers["/data/aircraft.json"] = SKY                                                                             # it hears again
    a.fetch()
    assert a.trouble() is None
    answers["/data/aircraft.json"] = {"aircraft": []}
    a.fetch()
    answers["/data/aircraft.json"] = None
    now[0] += 10 * aircraft.DEAF_S
    a.fetch()                                                                                                       # (it stopped answering: no claim about what it hears)
    assert a.trouble() is None


def test_the_watch_list_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    a = aircraft.Aircraft(get=_get({"/data/aircraft.json": SKY2, "/data/receiver.json": RECEIVER}))
    monkeypatch.setattr(aircraft, "aircraft", a)
    monkeypatch.setattr(aircraft, "ADDRESS", "pi:8080")
    monkeypatch.setattr(aircraft, "WATCH", "")

    def post(body):
        req = urllib.request.Request(base + "/api/aircraft", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        for bad in ({}, {"watch": 3}, {"watch": "#nope"}, {"watch": "x" * 201}):
            assert post(bad)[0] == 400
        code, d = post({"watch": "RRR, mil"})
        assert code == 200 and d["watch"] == "RRR, mil" and [p["call"] for p in d["watching"]] == ["RRR123"] and [p["call"] for p in d["low"]] == ["RRR123"]
        assert json.loads(conf.read_text())["aircraft_watch"] == "RRR, mil" and d["address"] == "pi:8080"
        assert post({"watch": ""})[1]["watching"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
