"""Aircraft overhead from a dump1090 / readsb receiver on the local network (playback/aircraft.py)."""
import json
import math
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


def test_the_scope_gets_the_aerials_place_and_each_aircrafts_for_its_map() -> None:
    a = _air()
    a.fetch()
    had = a.status(False)
    assert had["at"] == [54.532, -1.088]                                  # (the receiver's own place, to about 100 m: all the map needs)
    placed = [p for p in had["planes"] if "nm" in p]
    assert placed and all(isinstance(p["lat"], float) and isinstance(p["lon"], float) for p in placed)
    assert next(p for p in placed if p["hex"] == "440096")["lat"] == 55.364
    assert not any("lat" in p for p in had["planes"] if "nm" not in p)    # (no position, none shown)


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
    assert a.trouble() == []
    now[0] += aircraft.DEAF_S - 1
    a.fetch()
    assert a.trouble() == []
    now[0] += 2
    a.fetch()
    t = a.trouble()[0]
    assert t["key"] == "aircraft-deaf" and t["level"] == "warn" and "no aircraft for 1 h" in t["text"]
    clock = [NOW]
    h = health_mod.Health(Path("/x"), lambda: clock[0], lambda: 200.0, lambda: 45.0, lambda: False, lambda: 0, lambda: 30.0, lambda p: (160.0, 30.0), True)
    h.sample()
    assert [i["key"] for i in h.issues([a])["issues"]] == ["aircraft-deaf"]
    answers["/data/aircraft.json"] = SKY                                                                             # it hears again
    a.fetch()
    assert a.trouble() == []
    answers["/data/aircraft.json"] = {"aircraft": []}
    a.fetch()
    answers["/data/aircraft.json"] = None
    now[0] += 10 * aircraft.DEAF_S
    a.fetch()                                                                                                       # (it stopped answering: no claim about what it hears)
    assert a.trouble() == []


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
    monkeypatch.setattr(aircraft, "OFF", set())                                              # (the web side rebinds it: put it back after)

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
        assert code == 200 and d["watch"] == "RRR, mil" and [x["what"] for x in d["alerts"]] == ["aircraft:low:43c0a1", "aircraft:watch:43c0a1"] and d["flagged"][0]["kinds"] == ["low", "watch"]
        assert json.loads(conf.read_text())["aircraft_watch"] == "RRR, mil" and d["address"] == "pi:8080"
        assert all(x["says"] and x["short"] for x in d["alerts"]) and d["alerts"][0]["says"].startswith("Low aircraft")      # (the window says each alert in full)
        assert [x["what"] for x in post({"watch": ""})[1]["alerts"]] == ["aircraft:low:43c0a1"]
        assert post({"off": ["nonsense"]})[0] == 400 and post({"off": "low"})[0] == 400
        code, d = post({"off": ["low", "inbound"]})
        assert code == 200 and d["off"] == ["inbound", "low"] and d["alerts"] == [] and json.loads(conf.read_text())["aircraft_off"] == ["inbound", "low"]
        assert d["kinds"]["low"][0] == "Low and close" and d["address"] == "pi:8080"
    finally:
        httpd.shutdown()
        httpd.server_close()


LAT0, LON0 = RECEIVER["lat"], RECEIVER["lon"]


def spot(nm: float, deg: float) -> dict:
    """Where a place nm nautical miles from the aerial, on a bearing, is."""
    r = math.radians(deg)
    return {"lat": LAT0 + nm * math.cos(r) / 60, "lon": LON0 + nm * math.sin(r) / (60 * math.cos(math.radians(LAT0)))}


class Sky:
    """A receiver whose picture and clock the test moves."""
    def __init__(self, planes=(), **kw) -> None:
        self.now, self.planes = NOW, list(planes)
        self.a = aircraft.Aircraft(get=self.get, clock=lambda: self.now, address=lambda: "pi:8080", **kw)

    def get(self, url, timeout=6.0):
        if url.endswith("receiver.json"):
            return RECEIVER
        return {"aircraft": self.planes}

    def step(self, s: float = 0.0, planes=None):
        self.now += s
        if planes is not None:
            self.planes = list(planes)
        return self.a.fetch()


def _plane(hex_, call, nm, deg, ft=30000, kt=400, track=0, **kw) -> dict:
    return {"hex": hex_, "flight": call, "alt_baro": ft, "gs": kt, "track": track, **spot(nm, deg), **kw}


def test_heading_for_you() -> None:
    sky = Sky([_plane("a00001", "INB1", 6, 90, track=270, kt=300),                        # 6 nm east, flying west at 5 nm/min: over you in 1.2 min
               _plane("a00002", "AWAY1", 6, 90, track=90, kt=300),                        # ...flying away
               _plane("a00003", "MISS1", 10, 90, track=270, kt=300, **spot(0, 0))])        # (position replaced just below)
    sky.planes[2].update(spot(5, 0))                                                      # 5 nm north, flying west: passes 5 nm off
    sky.planes[2]["lon"] += 10 / 60 / math.cos(math.radians(LAT0))
    had = sky.step()
    assert [(p["call"], p["eta"]) for p in had["inbound"]] == [("INB1", 1.2)] and had["inbound"][0]["miss"] <= 0.2
    assert [x[3] for x in aircraft.alerts(had)] == ["Inbound: INB1 · 1.2 min"]
    aircraft.OFF.add("inbound")
    try:
        assert aircraft.alerts(had) == []
    finally:
        aircraft.OFF.clear()


def test_circling_is_noticed_after_a_quarter_of_an_hour_and_not_a_straight_line() -> None:
    sky = Sky()
    circler = lambda k: _plane("c00001", "HELI1", 12 + 2 * math.cos(k / 5), 40 + 7 * math.sin(k / 5), ft=1500, kt=80, track=(k * 20) % 360)
    runner = lambda k: _plane("c00002", "RUN1", 20 + k * 0.5, 200, kt=480, track=20)
    for k in range(41):                                                                    # 30 s apart: 20 minutes
        had = sky.step(30 if k else 0, [circler(k), runner(k)])
        if k == 20:
            assert had["loiter"] == []                                                     # (ten minutes in: not yet)
    assert [(p["call"], p["mins"]) for p in had["loiter"]] == [("HELI1", 20)]
    assert had["loiter"][0]["radius"] <= aircraft.LOITER_NM
    assert [x[3] for x in aircraft.alerts(had) if ":loiter:" in x[0]][0].startswith("Circling: HELI1 · 20 min")
    sky.step(600, [])                                                                       # it goes out of range: the track ends with it
    had = sky.step(30, [circler(1)])
    assert had["loiter"] == []


def test_falling_fast_twice_running_and_it_is_kept_for_a_while() -> None:
    sky = Sky()
    plane = lambda rate: _plane("d00001", "DROP1", 30, 10, ft=30000, baro_rate=rate)
    assert sky.step(0, [plane(-5000)])["descent"] == []                                    # (once: could be a blip)
    had = sky.step(30, [plane(-5200)])
    assert [(p["call"], p["rate"]) for p in had["descent"]] == [("DROP1", -5200)]
    assert "5,200 ft/min" in aircraft.alerts(had)[0][3] and aircraft.alerts(had)[0][1] == 2
    assert sky.step(30, [plane(-500)])["descent"]                                          # (it has levelled off: still shown)
    assert sky.step(aircraft.DESCENT_SHOWN_S + 1, [plane(-500)])["descent"] == []          # ...for ten minutes
    low = _plane("d00002", "DESC2", 30, 10, ft=5000, baro_rate=-6000)                      # (a normal approach: low already)
    sky.step(0, [low])
    assert sky.step(30, [low])["descent"] == []


def test_long_range_near_night_helicopters_and_heavies() -> None:
    sky = Sky()
    had = sky.step(0, [_plane("e00001", "FAR1", 280, 300), _plane("e00002", "FAKE1", 290, 300, mlat=["lat", "lon"]),
                       _plane("e00003", "NEAR1", 3, 100, ft=4000), _plane("e00004", "HELI2", 8, 10, ft=800, kt=70, category="A7"),
                       _plane("e00005", "HEAVY1", 7, 20, ft=6000, category="A5"), _plane("e00006", "HEAVY2", 7, 20, ft=36000, category="A5")])
    assert [p["call"] for p in had["long"]] == ["FAR1"]                                    # (a multilateration position is not trusted that far)
    assert [p["call"] for p in had["near"]] == ["NEAR1"] and [p["call"] for p in had["heli"]] == ["HELI2"] and [p["call"] for p in had["heavy"]] == ["HEAVY1"]
    kinds = lambda: [w.split(":")[1] for w, *_ in aircraft.alerts(had)]
    assert kinds() == ["long", "heli", "heavy"] and "night" not in kinds()                  # (it is daytime: no night flights)
    aircraft.QUIET = lambda: True
    try:
        assert kinds() == ["long", "night", "heli", "heavy"]
        aircraft.QUIET = lambda: 1 / 0                                                      # (a hook that fails is "not quiet")
        assert "night" not in kinds()
    finally:
        aircraft.QUIET = lambda: False
    assert sorted(p["call"] for p in aircraft.flagged(had)) == ["FAR1", "HEAVY1", "HELI2"]
    assert aircraft.flagged(None) == []


def test_a_busy_sky_needs_three_hours_of_watching() -> None:
    quiet = [_plane(f"b{k:05d}", f"Q{k}", 10 + k, 90) for k in range(10)]
    busy = [_plane(f"b{k:05d}", f"B{k}", 10 + k, 90) for k in range(30)]
    sky = Sky()
    for k in range(5):
        assert sky.step(1800 if k else 0, quiet)["busy"] is None
    assert sky.step(1800, busy)["busy"] is None                                             # (only five half hours behind it: not enough to know)
    sky.step(1800, quiet)
    had = sky.step(1800, busy)
    assert had["busy"] == {"n": 30, "usual": 10}
    assert aircraft.alerts(had)[0][:1] + aircraft.alerts(had)[0][3:] == ("aircraft:busy", "Busy: 30 aircraft (usually 10)")
    assert sky.step(1800, quiet)["busy"] is None


def test_range_that_has_dropped_tells_the_light() -> None:
    far, near = [_plane("f00001", "FAR", 100, 90)], [_plane("f00002", "NEAR", 20, 90)]
    sky = Sky()
    for k in range(26):                                                                     # a day's worth (13 h) at its usual 100 nm
        sky.step(1800 if k else 0, far)
    assert sky.a.trouble() == []
    for k in range(40):
        sky.step(100, near)                                                                 # the last hour and more: 20 nm
    t = sky.a.trouble()
    assert [x["key"] for x in t] == ["aircraft-range"] and "usually about 100 nm" in t[0]["text"]
    for k in range(22):                                                                     # it hears far again
        sky.step(100, far)
    assert sky.a.trouble() == []
    thin = Sky()
    for k in range(5):
        thin.step(1800 if k else 0, far)
    for k in range(22):
        thin.step(100, near)
    assert thin.a.trouble() == []                                                           # (too little history to say what is usual)


def test_alert_kinds_are_all_named_and_switchable() -> None:
    assert list(aircraft.KINDS) == ["low", "watch", "inbound", "loiter", "descent", "long", "night", "heli", "heavy", "busy"]
    assert all(len(v) == 2 and v[0] and v[1] for v in aircraft.KINDS.values())
    had = {"low": [{"hex": "x", "call": "L", "ft": 500, "nm": 1.0, "deg": 0}], "emergency": [{"hex": "y", "call": "E", "squawk": "7700"}]}
    assert [w.split(":")[1] for w, *_ in aircraft.alerts(had)] == ["y", "low"]
    aircraft.OFF.add("low")
    try:
        assert [w.split(":")[1] for w, *_ in aircraft.alerts(had)] == ["y"]                  # (an emergency squawk cannot be switched off)
    finally:
        aircraft.OFF.clear()
