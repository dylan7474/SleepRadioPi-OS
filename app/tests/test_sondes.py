"""Radiosondes from the receiver's background service on the local network (playback/sondes.py)."""
import json
import time
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi.playback import conditions, radio433, sondes
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0
AT = (54.532, -1.088)
STATUS = {"job": "sondes", "receiver_busy": False, "sonde_windows_utc": ["22:45-01:30", "10:45-13:30"]}


def flight(i="V1234567", alt=12400.0, vel_v=5.2, ago=3, lat=54.9, lon=-1.5, track=None, **kw):
    return {"id": i, "type": "RS41", "freq": "402.700 MHz", "lat": lat, "lon": lon, "alt": alt, "vel_v": vel_v, "vel_h": 21.0, "heading": 80.0, "temp": -38.5,
            "humidity": 12.0, "pressure": 190.0, "batt": 2.8, "snr": 14.2, "state": "climbing" if vel_v > 1 else "falling" if vel_v < -1 else "steady",
            "last": NOW - ago, "ago_s": ago, "track": track if track is not None else [[54.54, -1.09, 100.0], [lat, lon, alt]], **kw}


def _get(answers: dict):
    def get(url, timeout=6.0):
        for k, v in answers.items():
            if url.endswith(k):
                if v is None:
                    raise urllib.error.URLError("no route")
                return v(url) if callable(v) else v
        raise urllib.error.URLError("unknown " + url)
    return get


def _s(flights=None, status=None, address="192.0.2.10:8076", running=True):
    t = [NOW]
    answers = {"/sondes": {"running": running, "flights": flights if flights is not None else [flight()]}, "/status": status or STATUS}
    s = sondes.Sondes(get=_get(answers), clock=lambda: t[0], address=lambda: address)
    s.t, s.answers = t, answers
    return s


def test_it_shares_the_433_address_and_is_asked_only_with_one(monkeypatch) -> None:
    assert _s(address="").status() is None and not _s(address="").wanted()
    monkeypatch.setattr(radio433, "ADDRESS", "pi:8076")
    assert sondes.Sondes(get=_get({}), clock=lambda: NOW).wanted()           # (no address of its own: the 433 MHz window's)
    monkeypatch.setattr(radio433, "ADDRESS", "")
    assert not sondes.Sondes(get=_get({}), clock=lambda: NOW).wanted()


def test_a_flight_in_the_air(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    s = _s()
    assert s.fetch() is not None and s.LAN
    had = s.status(False)
    f = had["flights"][0]
    assert had["job"] == "sondes" and had["running"] and had["aerial"] == list(AT)
    assert f["id"] == "V1234567" and f["alt"] == 12400.0 and f["state"] == "climbing" and f["max_alt"] == 12400.0
    assert 47 < f["km"] < 51 and 320 < f["deg"] < 335                        # (41 km north and 26 km west of the aerial)
    assert f["temp"] == -38.5 and f["heading"] == 80.0 and len(f["track"]) == 2
    assert "climbing at 12.4 km" in had["says"] and f"{f['km']:g} km from you" in had["says"]
    s.t[0] += 30
    assert s.status(False)["flights"][0]["ago_s"] == 33                      # (heard 3 s before the look, and the look was 30 s ago)


def test_the_radio_keeps_the_flights_and_their_highest_point_in_memory(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    s = _s([flight(alt=30000.0, vel_v=5.0, track=[[54.5, -1.0, 100.0], [54.8, -1.4, 30000.0]])])
    s.fetch()
    s.t[0] += 20
    s.answers["/sondes"] = {"running": True, "flights": [flight(alt=29000.0, vel_v=-40.0, lat=54.9, lon=-1.7, track=[[54.5, -1.0, 100.0]])]}   # (burst: falling; the service's track is shorter)
    s.fetch()
    f = s.status(False)["flights"][0]
    assert f["max_alt"] == 30000.0 and f["state"] == "falling"
    assert len(f["track"]) == 3 and f["track"][-1] == [54.9, -1.7, 29000.0]  # (the radio's own track grew by the new point)
    s.answers["/sondes"] = {"running": False, "flights": []}                 # (the service lost it, or restarted: the radio still has it)
    s.t[0] += 20
    s.fetch()
    assert [x["id"] for x in s.status(False)["flights"]] == ["V1234567"]
    s.forget()
    assert s.status(False) is None


def test_only_the_newest_flights_are_kept(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: None)
    s = _s([flight(i=f"S{n}", ago=n) for n in range(12)])
    s.fetch()
    kept = s.status(False)["flights"]
    assert len(kept) == sondes.KEEP and [f["id"] for f in kept][:3] == ["S0", "S1", "S2"]
    assert "km" not in kept[0]                                               # (no aerial known: no distances)


def test_landed_and_lost(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    s = _s([flight(alt=900.0, vel_v=-6.0, ago=2)])
    s.fetch()
    s.answers["/sondes"] = {"running": True, "flights": [flight(alt=900.0, vel_v=-6.0, ago=2, track=[[54.5, -1.0, 100.0], [54.9, -1.5, 31000.0], [54.9, -1.5, 900.0]])]}
    s.t[0] += 5
    s.fetch()
    assert s.status(False)["flights"][0]["state"] == "falling"
    s.t[0] += sondes.LANDED_S + 10                                            # (the service keeps answering; the sonde has gone quiet)
    s.answers["/sondes"] = {"running": True, "flights": [flight(alt=900.0, vel_v=-6.0, ago=sondes.LANDED_S + 12, track=[[54.5, -1.0, 100.0], [54.9, -1.5, 31000.0], [54.9, -1.5, 900.0]])]}
    s.fetch()
    f = s.status(False)["flights"][0]
    assert f["state"] == "landed" and sondes.STATE_WORDS["landed"] == "landed (last heard)"     # (low, falling, then silent)
    high = _s([flight(i="H", alt=20000.0, vel_v=5.0)])
    high.fetch()
    high.t[0] += sondes.LANDED_S + 10
    high.answers["/sondes"] = {"running": True, "flights": [flight(i="H", alt=20000.0, vel_v=5.0, ago=sondes.LANDED_S + 12)]}
    high.fetch()
    assert high.status(False)["flights"][0]["state"] == "lost"               # (silent up high: lost, not landed)


def test_what_it_says_when_nothing_is_heard() -> None:
    s = _s([], status={**STATUS, "job": None, "receiver_busy": True})
    s.fetch()
    had = s.status(False)
    assert had["flights"] == [] and "Not listening for radiosondes now: the VHF receiver is in use" in had["says"] and "No radiosonde is being heard" in had["says"]
    assert sondes.sonde_word({"job": "sondes"}) == "Listening for radiosondes" and "scanning 433 MHz sensors" in sondes.sonde_word({"job": "scan433"})


def test_the_sonde_hours() -> None:
    w = ["22:45-01:30", "10:45-13:30"]
    assert sondes.next_window(w, 23 * 60) == ("22:45", "01:30", True) and sondes.next_window(w, 30) == ("22:45", "01:30", True)
    assert sondes.next_window(w, 12 * 60) == ("10:45", "13:30", True)
    assert sondes.next_window(w, 15 * 60) == ("22:45", "01:30", False) and sondes.next_window(w, 3 * 60) == ("10:45", "13:30", False)
    assert sondes.next_window([], 100) is None and sondes.next_window(["nonsense"], 100) is None


def test_the_tile(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    assert sondes.tile(None, set()) == []
    s = _s()
    s.fetch()
    t = sondes.tile(s.status(False), set(), minute=23 * 60)[0]
    assert t["key"] == "sondes" and t["title"] == "Radiosondes" and t["value"] == "1" and t["state"] == "good" and t["sub"] == "RS41 12.4 km, climbing"
    none = _s([])
    none.fetch()
    q = sondes.tile(none.status(False), set(), minute=23 * 60)[0]
    assert q["value"] == "0" and q["state"] == "quiet" and q["sub"] == "listening until 01:30 UTC"
    assert sondes.tile(none.status(False), set(), minute=15 * 60)[0]["sub"] == "next listening 22:45 UTC"
    old = _s()
    old.fetch()
    old.t[0] += 600
    old.answers["/sondes"] = {"running": True, "flights": [flight(ago=612)]}
    old.fetch()
    assert sondes.tile(old.status(False), set(), minute=15 * 60)[0]["sub"] == "last: RS41 lost (last heard)"


def test_it_is_in_the_keeper_and_in_the_world_outside(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    assert sondes.sondes in conditions.outside_feeds()
    s = _s()
    s.fetch()
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, sondes_now=s.status(False))
    assert "sondes" in [t["key"] for t in d["tiles"]]
    assert "sondes" not in [t["key"] for t in conditions.outside(conditions.summary({}, [], NOW), None, None, None, True)["tiles"]]


def test_from_the_web(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    real_gmtime = time.gmtime
    monkeypatch.setattr(time, "gmtime", lambda secs=None: real_gmtime(1794178800 if secs is None else secs))     # (23:00 UTC: the evening window is on, whatever the hour the tests run)
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    answers = {"/sondes": {"running": True, "flights": [flight()]}, "/status": STATUS, "/433": {"running": False, "messages": 0, "devices": []}}
    s = sondes.Sondes(get=_get(answers), address=lambda: radio433.ADDRESS)
    r = radio433.Radio433(get=_get(answers), address=lambda: radio433.ADDRESS)
    monkeypatch.setattr(sondes, "sondes", s)
    monkeypatch.setattr(radio433, "radio433", r)
    monkeypatch.setattr(radio433, "ADDRESS", "")

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return json.loads(resp.read())

    def post(body):
        req = urllib.request.Request(base + "/api/radio433", data=json.dumps(body).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    try:
        d = get("/api/sondes")
        assert d["up"] is False and d["flights"] == [] and d["address"] == ""
        assert "sondes" not in [t["key"] for t in get("/api/outside")["tiles"] if not t.get("empty") and t["key"] != "lightning"]
        assert post({"address": "pi.local:8076"})["up"] is True               # (the one address, set in the 433 MHz window, starts both)
        d = get("/api/sondes")
        assert d["up"] is True and d["flights"][0]["id"] == "V1234567" and d["job"] == "sondes" and d["now"] == "Listening for radiosondes"
        assert d["window"]["start"] == "22:45" and d["words"]["landed"] == "landed (last heard)" and d["aerial"] == list(AT)
        assert "sondes" in [t["key"] for t in get("/api/outside")["tiles"] if not t.get("empty") and t["key"] != "lightning"]
        assert post({"address": ""})["address"] == "" and get("/api/sondes")["flights"] == []       # (forgetting it forgets the flights too)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _flight_file(i="Y1722563", **kw):
    f = flight(i, alt=800.0, vel_v=-5.0, track=[[54.5, -1.0, 100.0], [54.9, -1.5, 26900.0], [55.0, -1.6, 800.0]], **kw)
    return {"format": sondes.FORMAT, "version": 1, "flights": [{"id": i, "first": NOW - 9000, "max_alt": 26907.0, "last_heard": NOW - 4000, "track": f["track"],
                                                                  **{k: f[k] for k in sondes.FIELDS}}]}


def test_a_flight_exports_and_imports_back_and_is_marked_imported(monkeypatch) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    s = _s()
    s.fetch()
    doc = s.export("V1234567")
    assert doc["format"] == sondes.FORMAT and doc["flights"][0]["id"] == "V1234567" and len(doc["flights"][0]["track"]) == 2
    s2 = _s(flights=[])
    assert s2.load(json.loads(json.dumps(doc))) == ["V1234567"]
    f = s2.kept()[0]
    assert f["imported"] is True and f["id"] == "V1234567" and f["max_alt"] == doc["flights"][0]["max_alt"] and "km" in f
    assert len(s.export()["flights"]) == 1
    try:
        s.export("nope")
        raise AssertionError
    except sondes.BadFlight:
        pass


def test_a_wrong_file_changes_nothing(monkeypatch) -> None:
    s = _s(flights=[])
    good = _flight_file()
    bad = _flight_file("X1")
    bad["flights"][0]["track"][1] = [200, 0, 5]                    # off the Earth
    both = {**good, "flights": good["flights"] + bad["flights"]}
    for data in (both, {"format": "other"}, [], {**good, "version": 9}, {**good, "flights": []},
                 {**good, "flights": [{**good["flights"][0], "id": "../../x"}]}, {**good, "flights": [{**good["flights"][0], "alt": float("nan")}]}):
        try:
            s.load(data)
            raise AssertionError(data)
        except sondes.BadFlight:
            pass
    assert s.kept() == []


def test_a_pinned_flight_survives_a_restart_and_the_limit_of_eight(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    pins = tmp_path / "state" / "pins.json"
    s = _s(flights=[flight(f"A{i}", ago=3 + i) for i in range(3)])
    s.pins_in(pins)
    s.fetch()
    assert not pins.exists()                                            # (nothing is written until something is pinned)
    s.pin("A1")
    assert pins.exists() and [f["id"] for f in json.loads(pins.read_text())["flights"]] == ["A1"]
    for n in range(12):                                                  # a stream of newer flights pushes the loose ones out, never the pinned
        s.t[0] += 60
        s.answers["/sondes"] = {"running": True, "flights": [flight(f"B{n}", ago=1)]}
        s.fetch()
    ids = [f["id"] for f in s.status(False)["flights"]]
    assert "A1" in ids and len([i for i in ids if i != "A1"]) == sondes.KEEP
    s2 = _s(flights=[])                                                  # a restart
    s2.pins_in(pins)
    f = s2.kept()[0]
    assert f["id"] == "A1" and f["pinned"] is True and len(f["track"]) >= 2
    s2.forget()                                                          # (changing the receiver's address forgets what it said, not the pinned)
    assert [f["id"] for f in s2.kept()] == ["A1"]
    s2.pin("A1", False)
    assert not pins.exists() and s2.kept()[0]["pinned"] is False


def test_the_pins_are_limited_and_a_flight_still_heard_is_written_again_once_it_is_silent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    pins = tmp_path / "pins.json"
    s = _s(flights=[flight(f"P{i}") for i in range(sondes.MAX_PINS + 1)])
    monkeypatch.setattr(sondes, "KEEP", 50)
    s.pins_in(pins)
    s.fetch()
    for i in range(sondes.MAX_PINS):
        s.pin(f"P{i}")
    try:
        s.pin(f"P{sondes.MAX_PINS}")
        raise AssertionError
    except sondes.BadFlight as e:
        assert "unpin" in str(e)
    for bad in ("nope",):
        try:
            s.pin(bad)
            raise AssertionError
        except sondes.BadFlight:
            pass
    before = json.loads(pins.read_text())["flights"]
    assert all(len(f["track"]) == 2 for f in before)
    s.answers["/sondes"] = {"running": True, "flights": [flight("P0", lat=55.0, track=[[54.54, -1.09, 100.0], [54.9, -1.5, 12400.0], [55.0, -1.5, 12400.0]], ago=1)]}
    s.t[0] += 10
    s.fetch()
    assert len(next(f for f in json.loads(pins.read_text())["flights"] if f["id"] == "P0")["track"]) == 2      # (still being heard: not written again yet)
    s.t[0] += sondes.ACTIVE_S + 10
    s.answers["/sondes"] = {"running": True, "flights": []}
    s.fetch()
    assert len(next(f for f in json.loads(pins.read_text())["flights"] if f["id"] == "P0")["track"]) == 3      # (silent now: written once)


def test_the_web_page_pins_exports_and_imports(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sondes, "aerial", lambda: AT)
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    s = _s(flights=[], address="")
    s.pins_in(tmp_path / "pins.json")
    monkeypatch.setattr(sondes, "sondes", s)
    monkeypatch.setattr(radio433, "ADDRESS", "")

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read()), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers
    try:
        code, d, _ = call("/api/sondes/import", _flight_file())
        assert code == 200 and d["imported"] == ["Y1722563"] and d["up"] is False and d["flights"][0]["imported"] is True    # (shown with no receiver too)
        assert call("/api/sondes/import", {"format": "x"})[0] == 400
        code, d, _ = call("/api/sondes/pin", {"id": "Y1722563", "pinned": True})
        assert code == 200 and d["flights"][0]["pinned"] is True and (tmp_path / "pins.json").exists()
        assert call("/api/sondes/pin", {"id": "nope", "pinned": True})[0] == 400 and call("/api/sondes/pin", {"id": "Y1722563"})[0] == 400
        code, d, h = call("/api/sondes/export?id=Y1722563")
        assert code == 200 and d["flights"][0]["id"] == "Y1722563" and "Y1722563.json" in h["Content-Disposition"]
        assert call("/api/sondes/export")[1]["format"] == sondes.FORMAT and call("/api/sondes/export?id=zz")[0] == 400
    finally:
        httpd.shutdown()
