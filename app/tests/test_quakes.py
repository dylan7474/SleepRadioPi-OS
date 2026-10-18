"""Earthquakes from the USGS and the BGS (playback/quakes.py), as tiles and alerts."""
import json
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import ft8
from sleepradiopi.playback import conditions, quakes
from sleepradiopi.web.server import make_handler

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc).timestamp()


def _usgs(*qs):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": i, "properties": {"mag": m, "place": place, "time": int(t * 1000), "url": f"https://usgs/{i}", "tsunami": ts, "type": "earthquake"},
         "geometry": {"type": "Point", "coordinates": [lon, lat, 10]}} for i, m, place, t, lat, lon, ts in qs]}


BGS = """<?xml version="1.0"?><rss version="2.0" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
<item><title>UK Earthquake alert : M  1.2 :GLENDARUEL,ARGYLL/BUTE, Tue, 13 Oct 2026 17:54:10</title>
<description>Origin date/time: Tue, 13 Oct 2026 17:54:10 ; Location: GLENDARUEL,ARGYLL/BUTE ; Lat/long: 56.032,-5.134 ; Depth: 11 km ; Magnitude:  1.2</description>
<link>http://earthquakes.bgs.ac.uk/earthquakes/recent_events/20261013175400.html</link><pubDate>Tue, 13 Oct 2026 17:54:10</pubDate>
<geo:lat>56.032</geo:lat><geo:long>-5.134</geo:long></item>
<item><title>UK Earthquake alert : M  2.3 :GUISBOROUGH,REDCAR AND CLEVELAND, Sun, 18 Oct 2026 14:10:00</title>
<description>Origin date/time: Sun, 18 Oct 2026 14:10:00 ; Location: GUISBOROUGH,REDCAR AND CLEVELAND ; Lat/long: 54.535,-1.056 ; Depth: 3 km ; Magnitude:  2.3</description>
<link>http://earthquakes.bgs.ac.uk/earthquakes/recent_events/20261018141000.html</link><pubDate>Sun, 18 Oct 2026 14:10:00</pubDate>
<geo:lat>54.535</geo:lat><geo:long>-1.056</geo:long></item>
<item><title>broken</title><description>nothing useful</description><pubDate>not a date</pubDate></item>
</channel></rss>"""


@pytest.fixture(autouse=True)
def _home_and_quiet(monkeypatch):
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(conditions, "_alerted", {})
    monkeypatch.setattr(conditions, "ALERTS", False)
    for name in ("sun", "forecast", "aurora"):
        monkeypatch.setattr(conditions, name, type(getattr(conditions, name))(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    from sleepradiopi.playback import spots, ukwarnings
    monkeypatch.setattr(ukwarnings, "warnings", ukwarnings.Warnings(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    monkeypatch.setattr(spots, "spots", spots.Spots(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))


def test_the_feeds_read() -> None:
    w = quakes.world(_usgs(("a", 5.21, "55 km NNW of Ruteng, Indonesia", NOW - 3600, -8.1, 120.3, 0),
                           ("b", 6.8, "near the east coast of Honshu, Japan", NOW - 600, 38.0, 142.0, 1)))
    assert [(q["id"], q["mag"], q["tsunami"]) for q in w] == [("b", 6.8, True), ("a", 5.2, False)]      # the biggest first
    assert quakes.world({"features": [{"properties": {"mag": None}}]}) == []
    uk = quakes.uk(ET.fromstring(BGS))
    assert [(q["place"], q["mag"]) for q in uk] == [("Guisborough, Redcar and Cleveland", 2.3), ("Glendaruel, Argyll/Bute", 1.2)]   # the latest first
    assert uk[0]["t"] == datetime(2026, 10, 18, 14, 10, tzinfo=timezone.utc).timestamp() and uk[0]["id"] == "bgs:20261018141000"
    assert quakes.km_from_home(54.535, -1.056) < 20 and 300 < quakes.km_from_home(56.032, -5.134) < 330
    assert quakes.km_from_home(0, 0, "") is None


def test_kept_and_said(tmp_path: Path) -> None:
    clock = [NOW]
    feeds = {"world": _usgs(("b", 6.8, "near the east coast of Honshu, Japan", NOW - 600, 38.0, 142.0, 1)), "uk": BGS}
    q = quakes.Quakes(get_json=lambda url: feeds["world"] or (_ for _ in ()).throw(OSError("x")), get_xml=lambda url: ET.fromstring(feeds["uk"]), clock=lambda: clock[0])
    q.keep_in(tmp_path / "quakes.json")
    had = q.fetch()
    assert len(had["world"]) == 1 and len(had["uk"]) == 2
    says = q._says(had)
    assert says.startswith("The largest earthquake in the last 12 hours: magnitude 6.8, near the east coast of Honshu, Japan, at ") and "A tsunami warning was issued." in says
    assert "In the UK: magnitude 2.3, at Guisborough, Redcar and Cleveland" in says and "Glendaruel" not in says      # (Tuesday's: not in the last 12 hours)
    feeds["world"] = None                                       # the USGS not reached: the BGS's still had
    assert "world" not in q.fetch() and q.status()["uk"]
    feeds["uk"] = "<broken"
    assert q.fetch() is None and q.status()["uk"]             # neither: what was had stands
    again = quakes.Quakes(clock=lambda: clock[0])
    again.keep_in(tmp_path / "quakes.json")
    assert again.status()["uk"][0]["mag"] == 2.3
    clock[0] += quakes.OLD_S + 1
    assert again.status() is None


def test_when_it_was_said() -> None:
    at = lambda *a: datetime(*a).timestamp()
    now = at(2026, 10, 18, 15, 0)
    assert quakes.when_said(at(2026, 10, 18, 14, 5), now) == "at 14:05"
    assert quakes.when_said(at(2026, 10, 17, 23, 0), now) == "yesterday"
    assert quakes.when_said(at(2026, 10, 13, 17, 54), now) == "on Tuesday"
    assert quakes.when_said(at(2026, 10, 3, 9, 0), now) == "on 3 October"


def test_which_are_alerts() -> None:
    had = {"world": quakes.world(_usgs(("big", 6.8, "near the east coast of Honshu, Japan", NOW - 600, 38.0, 142.0, 0),
                                       ("old", 7.1, "Fiji region", NOW - 5 * 3600, -18.0, 178.0, 0),
                                       ("small", 6.0, "Chile", NOW - 60, -30.0, -71.0, 0))),
           "uk": quakes.uk(ET.fromstring(BGS))}
    found = quakes.alerts(had, NOW)
    assert [(what, short) for what, _, _, short in found] == [
        ("quake:big", "M6.8 earthquake · Honshu, Japan"),                            # (6.5 and over, fresh; the old 7.1 isn't news, the 6.0 not big enough)
        ("ukquake:bgs:20261018141000", "UK earthquake M2.3 · Guisborough")]           # (2.3, but within 100 km of here)
    assert found[0][2].startswith("An earthquake: magnitude 6.8, near the east coast of Honshu, Japan, at ")
    far = {"uk": [{**had["uk"][0], "lat": 58.0, "lon": -5.0}]}                    # the same 2.3 in the Highlands: not
    assert quakes.alerts(far, NOW) == [] and quakes.alerts(None, NOW) == []
    assert quakes.alerts({"uk": [{**far["uk"][0], "mag": 3.1}]}, NOW)[0][0].startswith("ukquake:")   # ...unless it's 3 or more


def test_one_tile_for_the_last_twelve_hours() -> None:
    had = {"world": quakes.world(_usgs(("big", 7.2, "Kermadec Islands, New Zealand", NOW - 600, -30.0, -178.0, 0),
                                       ("older", 7.5, "Fiji region", NOW - 13 * 3600, -18.0, 178.0, 0))), "uk": quakes.uk(ET.fromstring(BGS))}
    t = quakes.tiles(had, set(), NOW)
    assert len(t) == 1 and (t[0]["key"], t[0]["value"], t[0]["state"]) == ("quakes", "M7.2", "poor")       # (Fiji's 7.5 was 13 hours ago: not shown)
    assert t[0]["sub"].startswith("UK: M2.3 Guisborough · ") and t[0]["says"].endswith("(USGS, British Geological Survey)")   # a UK one lately: under it
    no_uk = quakes.tiles({**had, "uk": [q for q in had["uk"] if "Glendaruel" in q["place"]]}, set(), NOW)[0]   # (Tuesday's: too old)
    assert no_uk["sub"].split(" · ")[0] == "Kermadec Islands, New Zealand"
    assert quakes.tiles({"world": []}, set(), NOW)[0]["sub"] == "none over 4.5 in 12 h" and quakes.tiles(None, set()) == []
    assert quakes.tiles(had, {"quakes"}, NOW)[0]["state"] == "alert"
    assert [q["id"] for q in quakes.lately(had, "world", NOW)] == ["big"] and len(quakes.lately(had, "uk", NOW)) == 1


def test_in_the_world_outside_and_said_once(monkeypatch) -> None:
    had = {"world": quakes.world(_usgs(("big", 6.8, "near the east coast of Honshu, Japan", NOW - 600, 38.0, 142.0, 0))), "uk": [], "t": NOW}

    class Fake:
        def status(self):
            return had
    monkeypatch.setattr(quakes, "quakes", Fake())
    monkeypatch.setattr(quakes.time, "time", lambda: NOW)
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, had)
    assert [t["key"] for t in d["tiles"]] == ["quakes"] and d["tiles"][0]["alert"] and d["alerts"][0]["tile"] == "quakes"
    monkeypatch.setattr(conditions, "ALERTS", True)
    at = datetime.fromtimestamp(NOW)
    assert conditions.alert_due(at, True, False, NOW).startswith("An earthquake: magnitude 6.8")
    assert conditions.alert_due(at, True, False, NOW + 60) is None                   # once


def test_the_earthquakes_window_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(quakes.time, "time", lambda: NOW)          # (the samples' "last 12 hours" is NOW's, whenever this runs)
    q = quakes.Quakes(get_json=lambda url: _usgs(("big", 6.8, "near Honshu", NOW - 600, 38.0, 142.0, 0)), get_xml=lambda url: ET.fromstring(BGS), clock=lambda: NOW)
    q.fetch()
    monkeypatch.setattr(quakes, "quakes", q)
    try:
        with urllib.request.urlopen(base + "/api/quakes", timeout=5) as r:
            d = json.loads(r.read())
        assert d["had"] and [x["mag"] for x in d["world"]] == [6.8] and d["uk"][0]["km"] < 20 and d["alerts"] is False
        assert d["alert_at"] == {"world": 6.5, "uk": 3.0, "near": 2.0, "near_km": 100}
        with urllib.request.urlopen(base + "/api/outside", timeout=5) as r:
            keys = [t["key"] for t in json.loads(r.read())["tiles"]]
        assert keys == ["quakes"]
        monkeypatch.setattr(quakes, "quakes", quakes.Quakes(get_json=lambda url: (_ for _ in ()).throw(OSError("offline")),
                                                           get_xml=lambda url: (_ for _ in ()).throw(OSError("offline"))))
        with urllib.request.urlopen(base + "/api/quakes", timeout=5) as r:      # no internet: nothing, and no fuss
            d = json.loads(r.read())
        assert d["had"] is False and d["world"] == [] and d["uk"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
