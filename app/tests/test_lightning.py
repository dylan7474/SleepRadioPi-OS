"""Lightning near here (playback/lightning.py): Blitzortung's feed read, the near ones kept, said, and the bell."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import ft8
from sleepradiopi.playback import conditions, lightning
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0
HOME = (54.5, -1.0)                       # the middle of IO94


def encode(s: str) -> str:
    """The other half of decode(): LZW, as Blitzortung's server squeezes its messages (for the tests)."""
    d, out, w, code = {}, [], s[0], 256
    for ch in s[1:]:
        if w + ch in d or len(w + ch) == 1:
            w += ch
            continue
        out.append(w if len(w) == 1 else chr(d[w]))
        d[w + ch] = code
        code += 1
        w = ch
    out.append(w if len(w) == 1 else chr(d[w]))
    return "".join(out)


def strike(lat, lon, t=NOW, **more) -> str:
    return encode(json.dumps({"time": int(t * 1e9), "lat": lat, "lon": lon, "alt": 0, "pol": 0, "mds": 9000, "mcg": 200, "status": 1,
                              "region": 1, "delay": 3.1, "lonc": 0, "latc": 0, "sig": [{"sta": 1, "time": 5, "lat": 50, "lon": 1, "alt": 0, "status": 1}], **more}))


@pytest.fixture(autouse=True)
def _set_up(monkeypatch):
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(lightning, "ON", True)
    monkeypatch.setattr(lightning, "BELL", True)
    monkeypatch.setattr(conditions, "_alerted", {})
    monkeypatch.setattr(conditions, "ALERTS", False)


class FakeWs:
    def __init__(self, messages, clock, step=1.0):
        self.messages, self.clock, self.step, self.sent = list(messages), clock, step, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, m):
        self.sent.append(m)

    def recv(self, timeout=None):
        self.clock[0] += self.step
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)


def test_the_feed_decoded() -> None:
    text = json.dumps({"time": 1792340000000000000, "lat": 54.6, "lon": -1.2, "sig": [{"sta": 1}, {"sta": 2}, {"sta": 1}]})
    squeezed = encode(text)
    assert len(squeezed) < len(text) and any(ord(c) > 255 for c in squeezed)
    assert lightning.decode(squeezed) == text and lightning.decode(text) == text and lightning.decode("") == ""
    assert lightning.decode("TOBEORNOT" + chr(256) + chr(258) + chr(260) + chr(265) + chr(259) + chr(261) + chr(263)) == "TOBEORNOTTOBEORTOBEORNOT"   # (the textbook case, KwKwK)


def test_only_the_near_ones_kept(tmp_path: Path) -> None:
    clock = [NOW]
    w = lightning.Watch(clock=lambda: clock[0])
    w.keep_in(tmp_path / "lightning.json")
    near = w.take(strike(54.6, -1.3))                           # Guisborough way: a few km
    assert near and near["km"] < 30 and lightning.compass(near["deg"]) in ("north-west", "west")
    assert w.take(strike(39.0, 3.5)) is None                    # Mallorca: not kept
    assert w.take("not json") is None and w.take(json.dumps({"lat": "x"})) is None
    assert w.take(strike(55.4, -1.0))["km"] == 100 or w.take(strike(55.5, -1.0)) is None    # (the edge: 100 km)
    clock[0] += lightning.KEEP_S + 1
    assert w.take(strike(54.4, -0.9, t=clock[0]))
    assert len(w.status()["strikes"]) == 1                        # the old ones gone
    ft8.HOME = None
    assert w.take(strike(54.6, -1.3)) is None                     # no locator: nothing's near


def test_a_spell_of_listening(monkeypatch) -> None:
    clock = [NOW]
    far = [strike(39.0, 3.5, t=NOW)] * 3
    ws = FakeWs(far, clock)
    w = lightning.Watch(connect=lambda host: ws, clock=lambda: clock[0])
    assert w.listen(spell=30) == 0 and ws.sent == ['{"a":111}']
    assert clock[0] - NOW <= 31 and w.heard == 3 and w.listened == clock[0] and not w.listening   # thirty seconds, then off
    clock[0] = NOW
    ws2 = FakeWs(far + [strike(54.6, -1.3, t=NOW + 4)], clock)     # a strike near: it stays on, STAY_S past it
    w2 = lightning.Watch(connect=lambda host: ws2, clock=lambda: clock[0])
    assert w2.listen(spell=30) == 1 and clock[0] - NOW >= lightning.STAY_S
    bad = lightning.Watch(connect=lambda host: (_ for _ in ()).throw(OSError("no route")), clock=lambda: clock[0])
    bad.listen()
    assert bad.error.startswith("ws1.blitzortung.org: OSError") and bad._host == 1                # the other server next time


def test_said_and_tiled() -> None:
    st = {"on": True, "home": "IO94", "area_km": 100, "t": NOW, "listened": NOW,
          "strikes": [{"t": NOW - 60, "km": 25, "deg": 225}, {"t": NOW - 120, "km": 31, "deg": 230}, {"t": NOW - 900, "km": 60, "deg": 230}]}
    a = lightning.alerts(st, NOW)
    assert a[0][0] == f"lightning:{round(NOW - 900)}" and a[0][2] == "Lightning 25 kilometres to the south-west, and getting closer."
    assert a[0][3] == "Lightning 25 km south-west"
    t = lightning.tile(st, set(), NOW)[0]
    assert (t["value"], t["state"], t["sub"]) == ("25 km", "poor", "3 in the hour · south-west")
    assert lightning.says(st, NOW).startswith("3 strikes within 100 km in the last hour; the nearest 25 km to the south-west")
    far = {**st, "strikes": [{"t": NOW - 60, "km": 70, "deg": 90}]}
    assert lightning.alerts(far, NOW) == [] and lightning.tile(far, set(), NOW)[0]["state"] == "fair"
    assert lightning.tile({**st, "strikes": []}, set(), NOW)[0]["value"] == "none"
    assert lightning.tile({**st, "strikes": [], "listened": None}, set(), NOW) == []              # (not listened yet: no tile)
    assert lightning.tile({**st, "on": False}, set(), NOW) == []


def test_the_bell_twice_a_storm_at_most(monkeypatch) -> None:
    clock, rung = [NOW], []
    w = lightning.Watch(clock=lambda: clock[0])
    w.on_bell = lambda: rung.append(clock[0])
    monkeypatch.setattr(lightning.threading, "Thread", lambda target, **k: type("T", (), {"start": lambda self: target()})())
    w.take(strike(54.9, -1.0, t=NOW))                            # 45 km: kept, no bell
    assert rung == []
    w.take(strike(54.8, -1.0, t=NOW + 60)); clock[0] += 60       # 33 km: within 40 -- the bell
    assert len(rung) == 1
    w.take(strike(54.75, -1.0, t=NOW + 120)); clock[0] += 60     # nearer, but still the same storm: not again
    assert len(rung) == 1
    w.take(strike(54.55, -1.0, t=NOW + 180)); clock[0] += 60     # within 10 km: again, once
    w.take(strike(54.52, -1.0, t=NOW + 240)); clock[0] += 60
    assert len(rung) == 2
    clock[0] += 2 * 3600                                         # hours later, a new storm: the bell again
    w.take(strike(54.8, -1.0, t=clock[0]))
    assert len(rung) == 3
    lightning.BELL = False
    clock[0] += 2 * 3600
    w.take(strike(54.8, -1.0, t=clock[0]))
    assert len(rung) == 3                                        # the bell off: none


def test_the_bell_itself() -> None:
    from sleepradiopi.audio import pcm
    b = lightning.bell()
    assert b.dtype.name == "int16" and b.shape[1] == pcm.CHANNELS and 3 * pcm.SAMPLE_RATE < len(b) < 8 * pcm.SAMPLE_RATE
    assert abs(int(b[-100:].astype(int).sum())) < 100 * 50                                    # (faded out at the end)


def test_the_bell_keeps_the_quiet_hours(tmp_path: Path, monkeypatch) -> None:
    from sleepradiopi import main as main_mod
    from test_offline import _station
    st = _station(tmp_path / "st")
    played = []
    control = type("C", (), {"play_clip": lambda self, clip: played.append(clip.label)})()
    st.set_quiet(True, 0, 0)                                     # (start == end: no quiet at all)
    main_mod._lightning_bell(st, control)
    assert played == ["Lightning"]
    st.set_quiet(True, 0, 24 * 60 - 1)                           # quiet all day
    main_mod._lightning_bell(st, control)
    assert played == ["Lightning"]
    st.set_quiet(False)                                          # quiet hours off: rings
    main_mod._lightning_bell(st, control)
    assert played == ["Lightning", "Lightning"]
    with pytest.raises(ValueError):
        st.set_quiet(start_min=1440)
    assert st.news_schedule.quiet is None and st.birthdays.quiet is None
    st.set_quiet(True, 22 * 60, 7 * 60)
    assert str(st.news_schedule.quiet.start)[:5] == "22:00" and st.birthdays.quiet is st.news_schedule.quiet
    assert (st.dj_settings()["quiet_on"], st.dj_settings()["quiet_start_min"], st.dj_settings()["quiet_end_min"]) == (True, 1320, 420)


def test_in_the_world_outside_and_alerts(monkeypatch) -> None:
    st = {"on": True, "home": "IO94", "area_km": 100, "t": NOW, "listened": NOW, "strikes": [{"t": NOW - 60, "km": 25, "deg": 225}]}
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, light_now=st)
    assert d["tiles"][0]["key"] == "lightning" and d["tiles"][0]["state"] == "alert" and d["alerts"][0]["tile"] == "lightning"
    monkeypatch.setattr(lightning, "watch", type("W", (), {"status": lambda self: st})())
    monkeypatch.setattr(conditions, "ALERTS", True)
    for name in ("sun", "forecast", "aurora"):
        monkeypatch.setattr(conditions, name, type(getattr(conditions, name))(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    from sleepradiopi.playback import quakes, ukwarnings
    monkeypatch.setattr(quakes, "quakes", quakes.Quakes(get_json=lambda u: (_ for _ in ()).throw(OSError("x")), get_xml=lambda u: (_ for _ in ()).throw(OSError("x"))))
    monkeypatch.setattr(ukwarnings, "warnings", ukwarnings.Warnings(get=lambda u: (_ for _ in ()).throw(OSError("x"))))
    at = datetime.fromtimestamp(NOW)
    assert conditions.alert_due(at, True, False, NOW) == "Lightning 25 kilometres to the south-west."
    assert conditions.alert_due(at, True, False, NOW + 60) is None


def test_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    rung = []
    speaker = type("S", (), {"play_clip": lambda self, clip: rung.append(clip.label)})()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, speaker, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/lightning", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        with urllib.request.urlopen(base + "/api/lightning", timeout=5) as r:
            d = json.loads(r.read())
        assert d["on"] and d["bell"] and d["area_km"] == 100 and d["alert_km"] == 40 and d["close_km"] == 10 and d["alerts"] is False
        for bad in ({}, {"on": 1}, {"bell": "yes"}):
            assert post(bad)[0] == 400
        assert post({"on": False}) == (200, {"on": False, "bell": True}) and json.loads(conf.read_text())["lightning_watch"] is False
        assert post({"bell": False})[1]["bell"] is False and json.loads(conf.read_text())["lightning_bell"] is False
        assert post({"ring": True})[0] == 200 and rung == ["Lightning"]                         # hear it, whatever the settings
    finally:
        httpd.shutdown()
        httpd.server_close()
