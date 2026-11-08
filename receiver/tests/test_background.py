"""The background jobs' own logic (no dongle, no programs, no network but the loopback)."""
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import sleepradio_background as bgm  # noqa: E402
import sleepradio_receiver as rx  # noqa: E402

WINDOWS = [bgm.parse_window("22:45-01:30"), bgm.parse_window("10:45-13:30")]


def minute(h: int, m: int = 0) -> int:
    return h * 60 + m


def test_windows_run_past_midnight() -> None:
    assert WINDOWS == [(1365, 90), (645, 810)]
    for h, m, want in ((22, 44, False), (22, 45, True), (23, 59, True), (0, 0, True), (1, 29, True), (1, 30, False),
                       (10, 44, False), (10, 45, True), (13, 29, True), (13, 30, False), (18, 0, False)):
        assert bgm.in_windows(minute(h, m), WINDOWS) is want, (h, m)
    for bad in ("22:45", "25:00-01:00", "a-b", "10:00-11:61x"):
        with pytest.raises(ValueError):
            bgm.parse_window(bad)


def test_the_receiver_comes_first_and_a_flight_keeps_the_sonde_job() -> None:
    assert bgm.choose(minute(23), WINDOWS, receiver_busy=True, sonde_heard=True) is None
    assert bgm.choose(minute(23), WINDOWS, False, False) == "sondes"
    assert bgm.choose(minute(15), WINDOWS, False, False) == "scan433"
    assert bgm.choose(minute(15), WINDOWS, False, True) == "sondes"      # (past the window, still hearing one)
    assert bgm.choose(minute(15), [], False, False) == "scan433"


def test_433_devices_are_anonymous_unless_they_are_yours() -> None:
    d = bgm.Devices(b"salt", {"Acurite-Tower:1234": "Shed"})
    assert not d.feed("not json") and not d.feed("[1]") and not d.feed('{"id": 3}')
    for _ in range(3):
        assert d.feed(json.dumps({"model": "Acurite-Tower", "id": 1234, "temperature_C": 7.5, "humidity": 80, "rssi": -9.4, "freq": 433.92}), now=100)
    assert d.feed(json.dumps({"model": "Nexus-TH", "id": 77, "temperature_C": 21.0, "rssi": -15}), now=101)
    snap = d.snapshot(now=110)
    assert snap["messages"] == 4 and len(snap["devices"]) == 2
    shed = next(e for e in snap["devices"] if e["model"] == "Acurite-Tower")
    other = next(e for e in snap["devices"] if e["model"] == "Nexus-TH")
    assert shed["name"] == "Shed" and shed["readings"] == {"temperature_C": 7.5, "humidity": 80} and shed["count"] == 3 and shed["ago_s"] == 10
    assert "readings" not in other and "name" not in other
    assert "1234" not in json.dumps(snap) and "77" not in other["id"] and len(other["id"]) == 8
    assert bgm.Devices(b"other salt").snapshot()["devices"] == []        # (a different salt, a different made-up ID)
    d2 = bgm.Devices(b"other salt")
    d2.feed(json.dumps({"model": "Nexus-TH", "id": 77}))
    assert d2.snapshot()["devices"][0]["id"] != other["id"]


def test_433_remembers_a_limited_number_and_turns_its_log_over(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bgm, "MAX_DEVICES", 5)
    monkeypatch.setattr(bgm, "LOG_LIMIT", 300)
    d = bgm.Devices(b"s", log_path=tmp_path / "433.jsonl")
    for i in range(12):
        d.feed(json.dumps({"model": "M", "id": i}), now=1000 + i)
    assert len(d.snapshot()["devices"]) == 5
    assert (tmp_path / "433.old").exists() and (tmp_path / "433.jsonl").stat().st_size <= 400
    assert json.loads((tmp_path / "433.jsonl").read_text().splitlines()[-1])["model"] == "M"
    d.log_path = tmp_path / "no" / "such" / "dir" / "x.jsonl"
    assert d.feed(json.dumps({"model": "M", "id": 99}))                  # (an unwritable log doesn't stop it)
    assert d.log_path is None


def test_flights_from_the_decoders_archive() -> None:
    archive = {
        "V1234567": {"timestamp": 990, "path": [[54.0 + i / 1000, -1.0, 100 * i] for i in range(1000)],
                     "latest_telem": {"id": "V1234567", "type": "RS41", "freq": "402.700 MHz", "lat": 54.9, "lon": -1.0, "alt": 30000,
                                      "vel_v": 5.2, "vel_h": 20.1, "temp": -45.0, "snr": 12.5}},
        "D9": {"timestamp": 500, "path": [], "latest_telem": {"id": "D9", "type": "DFM09", "lat": 55.0, "lon": -2.0, "alt": 800, "vel_v": -9.0}},
        "broken": {"timestamp": 1},
    }
    flights = bgm.flights_from(archive, now=1000)
    assert [f["id"] for f in flights] == ["V1234567", "D9"]
    assert flights[0]["state"] == "climbing" and flights[0]["ago_s"] == 10 and len(flights[0]["track"]) <= bgm.TRACK_POINTS
    assert flights[1]["state"] == "falling" and flights[1]["track"] == []
    assert bgm.flights_from({}) == [] and bgm.flights_from(None) == []


class FakeJob:
    def __init__(self, name: str) -> None:
        self.name, self.proc, self.died, self.running, self.starts = name, None, 0.0, False, 0

    @property
    def alive(self) -> bool:
        return self.running

    def start(self) -> None:
        self.running, self.proc, self.starts = True, object(), self.starts + 1

    def stop(self, wait_s: float = 0) -> None:
        self.running, self.proc = False, None


def make(clock, busy, archive=None, windows=WINDOWS):
    jobs = {"sondes": FakeJob("sondes"), "scan433": FakeJob("scan433")}
    return bgm.Background(jobs, windows, lambda: busy[0], lambda: archive or {}, bgm.Devices(b"s"), clock=clock), jobs


def utc(h: int, m: int = 0) -> float:
    return 1_800_000_000 - 1_800_000_000 % 86400 + h * 3600 + m * 60


def test_jobs_follow_the_hours_and_the_receiver() -> None:
    now, busy = [utc(15)], [False]
    bg, jobs = make(lambda: now[0], busy)
    bg.tick()
    assert bg.current == "scan433" and jobs["scan433"].running and not jobs["sondes"].running
    now[0] = utc(22, 50)
    bg.tick()
    assert bg.current == "sondes" and jobs["sondes"].running and not jobs["scan433"].running
    busy[0] = True                                                      # a listener on the receiver: everything lets go
    bg.tick()
    assert bg.current is None and not jobs["sondes"].running and not jobs["scan433"].running
    busy[0] = False                                                     # (the receiver rested)
    bg.tick()
    assert bg.current == "sondes" and jobs["sondes"].running


def test_yield_stops_the_job_and_holds_it_off() -> None:
    now, busy = [utc(15)], [False]
    bg, jobs = make(lambda: now[0], busy)
    bg.tick()
    assert bg.make_way() == "scan433" and not jobs["scan433"].running and bg.current is None
    now[0] += 10
    bg.tick()                                                           # (the receiver isn't showing as busy yet: it is about to open the dongle)
    assert bg.current is None and jobs["scan433"].starts == 1
    now[0] += bgm.HOLD_OFF_S
    bg.tick()
    assert bg.current == "scan433" and jobs["scan433"].starts == 2
    assert bg.make_way() == "scan433" and bg.make_way() is None


def test_a_flight_outlasts_its_window_and_a_dead_job_waits_before_a_restart() -> None:
    now, busy = [utc(1, 29)], [False]
    archive = {"V1": {"timestamp": utc(1, 29), "path": [], "latest_telem": {"id": "V1", "lat": 54.5, "lon": -1.0, "alt": 20000, "vel_v": -9.0}}}
    bg, jobs = make(lambda: now[0], busy, archive)
    bg.tick()
    assert bg.current == "sondes"
    bg.tick()
    assert bg.sondes()["flights"][0]["id"] == "V1" and bg.sondes()["running"]
    now[0] = utc(1, 40)                                                 # (past the window, the flight heard 11 minutes ago: over)
    bg.tick()
    assert bg.current == "scan433"
    now[0] = utc(1, 31)
    bg.sonde_heard_at = utc(1, 29)                                      # ...but 2 minutes after, it would still be the sonde job
    bg.tick()
    assert bg.current == "sondes"
    jobs["sondes"].running = False                                      # the decoder died
    now[0] += 1
    bg.tick()
    assert jobs["sondes"].starts == 2 or not jobs["sondes"].running
    starts = jobs["sondes"].starts
    now[0] += 5
    bg.tick()
    assert jobs["sondes"].starts == starts                              # (not at once again)
    now[0] += bgm.RESTART_AFTER_S
    bg.tick()
    assert jobs["sondes"].running


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


def get(url: str, method: str = "GET"):
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def test_the_web_side() -> None:
    now, busy = [utc(15)], [False]
    bg, jobs = make(lambda: now[0], busy)
    bg.tick()
    bg.devices.feed(json.dumps({"model": "Nexus-TH", "id": 5, "rssi": -12}))
    httpd, url = serve(bgm.make_handler(bg))
    try:
        assert get(url + "/status")["job"] == "scan433" and get(url + "/status")["sonde_windows_utc"] == ["22:45-01:30", "10:45-13:30"]
        assert get(url + "/433")["running"] and get(url + "/433")["devices"][0]["model"] == "Nexus-TH"
        assert get(url + "/sondes") == {"running": False, "flights": []}
        assert get(url + "/yield", "POST") == {"ok": True, "was": "scan433"} and not jobs["scan433"].running
        with pytest.raises(urllib.error.HTTPError) as e:
            get(url + "/nothing")
        assert e.value.code == 404
    finally:
        httpd.shutdown()


def test_the_receiver_asks_before_it_opens_the_dongle() -> None:
    asked = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            asked.append(self.path)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd, url = serve(H)
    try:
        r = rx.Receiver(rx.DEFAULT_BANDS, background=url)
        r._make_way()
        assert asked == ["/yield"]
    finally:
        httpd.shutdown()
    rx.Receiver(rx.DEFAULT_BANDS, background="http://127.0.0.1:9")._make_way()      # nobody there: no trouble
    rx.Receiver(rx.DEFAULT_BANDS)._make_way()


def test_the_receivers_status_says_whether_it_is_busy() -> None:
    answers = [{"running": False, "listeners": 0}, {"running": True, "listeners": 0}, {"running": False, "listeners": 1}]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answers[0]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd, url = serve(H)
    try:
        busy = bgm.receiver_busy_check(url)
        got = []
        for a in answers:
            answers[0] = a
            got.append(busy())
        assert got == [False, True, True]
    finally:
        httpd.shutdown()
    assert bgm.receiver_busy_check("http://127.0.0.1:9")() is False


def test_the_known_devices_are_read_never_written(tmp_path: Path) -> None:
    (tmp_path / "known.json").write_text('{"Acurite-Tower:1234": "Shed"}')
    assert bgm.load_known(tmp_path / "known.json") == {"Acurite-Tower:1234": "Shed"}
    assert bgm.load_known(tmp_path / "nope.json") == {}
    assert not hasattr(bgm, "load_salt")                                # (the salt is made at every start: no file)


def test_the_decoders_logs_in_ram_are_kept_small(tmp_path: Path) -> None:
    (tmp_path / "log_power_X.csv").write_text("x" * 5000)
    (tmp_path / "small.log").write_text("keep")
    assert bgm.tidy(tmp_path, limit=1000) == 1
    assert (tmp_path / "log_power_X.csv").read_text() == "" and (tmp_path / "small.log").read_text() == "keep"
    assert bgm.tidy(tmp_path / "missing") == 0
