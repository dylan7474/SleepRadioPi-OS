"""Weather and flood warnings (playback/ukwarnings.py) and who heard my callsign (playback/spots.py)."""
import json
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import ft8
from sleepradiopi.playback import conditions, quakes, room, spots, ukwarnings
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0

MET = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Met Office warnings for North East England</title>
<item><title>Yellow warning of rain affecting North East England</title>
<link>https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-warnings#?id=aaa</link>
<description>Yellow warning of rain affecting North East England: Darlington, Durham, Redcar and Cleveland valid from 0600 Tue 20 Oct to 2100 Tue 20 Oct</description>
<guid isPermaLink="false">aaa</guid></item>
<item><title>Amber warning of wind affecting North East England</title>
<link>https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-warnings#?id=bbb</link>
<description>Amber warning of wind affecting North East England: Northumberland valid from 1200 Tue 20 Oct to 0600 Wed 21 Oct</description>
<guid isPermaLink="false">bbb</guid></item>
<item><title>Something else entirely</title></item>
</channel></rss>"""

EA = {"items": [
    {"description": "River Leven at Great Ayton", "floodAreaID": "122WAF942", "severityLevel": 2, "severity": "Flood warning",
     "floodArea": {"riverOrSea": "River Leven"}, "timeSeverityChanged": "2026-10-20T08:00:00", "message": "River levels are rising at Great Ayton."},
    {"description": "Lower River Tees", "floodAreaID": "122FWT001", "severityLevel": 3, "severity": "Flood alert", "floodArea": {"riverOrSea": "River Tees"}},
    {"description": "Old one", "floodAreaID": "x", "severityLevel": 4, "severity": "Warning no longer in force"}]}

PSK = """<?xml version="1.0"?><receptionReports>
<receptionReport receiverCallsign="DL1ABC" receiverLocator="JO31" senderCallsign="M8ODJ" frequency="14074500" flowStartSeconds="1792339000" mode="FT8" receiverDXCC="Fed. Rep. of Germany" sNR="-12" />
<receptionReport receiverCallsign="DL1ABC" receiverLocator="JO31" senderCallsign="M8ODJ" frequency="7074500" flowStartSeconds="1792300000" mode="FT8" sNR="-20" />
<receptionReport receiverCallsign="VK2XYZ" receiverLocator="QF56" senderCallsign="M8ODJ" frequency="14074900" flowStartSeconds="1792330000" mode="FT8" receiverDXCC="Australia" sNR="-21" />
<receptionReport receiverCallsign="G4ZZZ" senderCallsign="M8ODJ" frequency="50313000" flowStartSeconds="1792335000" mode="FT8" sNR="3" />
</receptionReports>"""


def _offline(*a, **k):
    raise OSError("offline")


@pytest.fixture(autouse=True)
def _set_up(monkeypatch):
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(room, "CALLSIGN", "M8ODJ")
    monkeypatch.setattr(ukwarnings, "REGION", "")
    monkeypatch.setattr(conditions, "_alerted", {})
    monkeypatch.setattr(conditions, "ALERTS", False)
    for name in ("sun", "forecast", "aurora"):
        monkeypatch.setattr(conditions, name, type(getattr(conditions, name))(get=_offline))
    monkeypatch.setattr(quakes, "quakes", quakes.Quakes(get_json=_offline, get_xml=_offline))
    monkeypatch.setattr(ukwarnings, "warnings", ukwarnings.Warnings(get=_offline))
    monkeypatch.setattr(spots, "spots", spots.Spots(get=_offline))


def _answers(urls: dict, asked: list):
    def get(url):
        asked.append(url)
        for k, v in urls.items():
            if url.startswith(k):
                if v is None:
                    raise urllib.error.URLError("no route")
                return v.encode() if isinstance(v, str) else json.dumps(v).encode()
        raise urllib.error.URLError("unknown " + url)
    return get


def test_the_region_by_the_locator() -> None:
    at = lambda grid: ukwarnings.region_for(ft8.locate(grid))
    assert (at("IO94"), at("IO91"), at("IO75"), at("IO64"), at("JO02"), at("IO70"), at("IO83")) == ("ne", "se", "st", "ni", "ee", "sw", "nw")
    assert at("JN18") is None                                     # (Paris: no Met Office region)
    ukwarnings.REGION = "yh"
    assert at("IO94") == "yh" and ukwarnings.region_for() == "yh"   # chosen: that one


def test_the_feeds_read() -> None:
    w = ukwarnings.met(ET.fromstring(MET))
    assert [(x["level"], x["what"], x["id"]) for x in w] == [("amber", "wind", "bbb"), ("yellow", "rain", "aaa")]   # the most serious first
    assert w[0]["valid"] == "1200 Tue 20 Oct to 0600 Wed 21 Oct"
    f = ukwarnings.floods(EA)
    assert [(x["kind"], x["where"]) for x in f] == [("flood warning", "River Leven at Great Ayton"), ("flood alert", "Lower River Tees")]
    assert f[0]["url"] == "https://check-for-flooding.service.gov.uk/target-area/122WAF942"


def test_kept_tiled_and_said(tmp_path: Path) -> None:
    asked = []
    urls = {"https://www.metoffice.gov.uk/": MET, "https://environment.data.gov.uk/": EA}
    w = ukwarnings.Warnings(get=_answers(urls, asked), clock=lambda: NOW)
    w.keep_in(tmp_path / "warnings.json")
    had = w.fetch()
    assert asked[0].endswith("/Region/ne") and "lat=54.500&long=-1.000&dist=30" in asked[1]
    assert had["region"] == "ne" and len(had["met"]) == 2 and len(had["floods"]) == 2
    says = ukwarnings.says(had)
    assert says.startswith("An amber warning of wind for North East England, valid from 1200 Tue 20 Oct to 0600 Wed 21 Oct.")
    assert "A flood warning: River Leven at Great Ayton." in says
    t = ukwarnings.tile(had, set())[0]
    assert (t["value"], t["state"], t["sub"]) == ("AMBER", "fair", "wind · and 3 more")
    flood_only = {**had, "met": [had["met"][1]]}                 # a flood warning beats a yellow
    assert ukwarnings.tile(flood_only, set())[0]["value"] == "FLOOD"
    assert ukwarnings.tile({"region": "ne", "met": [], "floods": []}, set())[0]["sub"] == "none for North East England"
    urls["https://www.metoffice.gov.uk/"] = None                 # the Met Office not reached: the floods still had
    assert "met" not in w.fetch()
    urls["https://environment.data.gov.uk/"] = None
    assert w.fetch() is None and w.status()["floods"]           # neither: what was had stands
    ft8.HOME = None
    assert ukwarnings.Warnings(get=_answers(urls, asked)).fetch() is None     # no locator, no region: nothing to ask


def test_warning_alerts() -> None:
    had = {"region": "ne", "met": ukwarnings.met(ET.fromstring(MET)), "floods": ukwarnings.floods(EA)}
    found = ukwarnings.alerts(had)
    assert [(what, short) for what, _, _, short in found] == [("met:bbb:amber", "Amber warning: wind"),
                                                              ("flood:122WAF942:2", "Flood warning: River Leven at Great Ayton")]   # (not the yellow, not the flood alert)
    assert found[0][2] == "The Met Office has issued an amber warning of wind for North East England, valid from 1200 Tue 20 Oct to 0600 Wed 21 Oct."


def test_said_once_and_again_when_raised(monkeypatch) -> None:
    had = {"region": "ne", "met": ukwarnings.met(ET.fromstring(MET)), "floods": [], "t": NOW}

    class Fake:
        def status(self):
            return had
    monkeypatch.setattr(ukwarnings, "warnings", Fake())
    monkeypatch.setattr(conditions, "ALERTS", True)
    at = datetime.fromtimestamp(NOW)
    assert conditions.alert_due(at, True, False, NOW).startswith("The Met Office has issued an amber warning of wind")
    assert conditions.alert_due(at, True, False, NOW + 60) is None
    had["met"] = [{**had["met"][0], "level": "red"}]               # raised: said again
    assert conditions.alert_due(at, True, False, NOW + 120).startswith("The Met Office has issued a red warning of wind")
    d = conditions.outside(conditions.summary({}, [], NOW), None, None, None, True, None, had, None)
    assert d["tiles"][0]["key"] == "warnings" and d["tiles"][0]["state"] == "alert" and d["alerts"][0]["tile"] == "warnings"


def test_who_heard_me() -> None:
    sp = spots.read(ET.fromstring(PSK), "IO94")
    assert [s["call"] for s in sp] == ["DL1ABC", "G4ZZZ", "VK2XYZ"]                      # the latest from each, the latest first
    assert (sp[0]["band"], sp[0]["snr"], sp[0]["country"]) == ("20 m", -12, "Fed. Rep. of Germany") and 500 < sp[0]["km"] < 700
    assert sp[1]["band"] == "6 m" and "km" not in sp[1] and sp[2]["km"] > 16000
    assert (spots.band(14074000), spots.band(144174000), spots.band(1e6), spots.band(None)) == ("20 m", "2 m", "1.000 MHz", None)
    had = {"call": "M8ODJ", "spots": sp}
    assert spots.said(had) == "M8ODJ was heard by 3 stations in the last day, the farthest VK2XYZ at " + f"{sp[2]['km']:,}" + " km (20 m, FT8)."
    t = spots.tile(had, 1792339000 + 600)[0]
    assert (t["value"], t["state"], t["sub"]) == ("3 heard", "good", f"farthest {sp[2]['km']:,} km · 20 m")
    assert spots.tile(had, 1792339000 + 5 * 3600)[0]["state"] == "fair"                   # (heard, but not lately)
    assert spots.tile({"call": "M8ODJ", "spots": []})[0]["sub"] == "not heard today" and spots.tile(None) == []


def test_spots_asked_for_my_callsign_only(tmp_path: Path) -> None:
    asked, call = [], ["M8ODJ"]
    s = spots.Spots(get=lambda url: asked.append(url) or ET.fromstring(PSK), clock=lambda: NOW, call=lambda: call[0])
    assert s.fetch()["call"] == "M8ODJ"
    assert "senderCallsign=M8ODJ" in asked[0] and "flowStartSeconds=-86400" in asked[0] and "appcontact=https%3A%2F%2Fgithub.com" in asked[0]
    assert "dylanjones" not in asked[0]                                                    # (the project, never the owner's address)
    call[0] = "G0XYZ"
    assert s.status() is None                                                              # someone else's now: not shown
    call[0] = None
    assert s.fetch() is None                                                               # no callsign: nothing asked


def test_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    w = ukwarnings.Warnings(get=_answers({"https://www.metoffice.gov.uk/": MET, "https://environment.data.gov.uk/": EA}, []))
    w.fetch()
    monkeypatch.setattr(ukwarnings, "warnings", w)
    s = spots.Spots(get=lambda url: ET.fromstring(PSK))
    s.fetch()
    monkeypatch.setattr(spots, "spots", s)

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        with urllib.request.urlopen(base + "/api/warnings", timeout=5) as r:
            d = json.loads(r.read())
        assert d["region"] == "ne" and d["chosen"] == "" and len(d["met"]) == 2 and len(d["floods"]) == 2 and d["regions"]["ne"] == "North East England"
        with urllib.request.urlopen(base + "/api/spots", timeout=5) as r:
            d = json.loads(r.read())
        assert d["call"] == "M8ODJ" and [x["call"] for x in d["spots"]][0] == "DL1ABC" and "callsign=M8ODJ" in d["map"]
        with urllib.request.urlopen(base + "/api/outside", timeout=5) as r:
            keys = [t["key"] for t in json.loads(r.read())["tiles"]]
        assert keys == ["warnings", "signal", "qrz"]
        for bad in ({}, {"region": "xx"}, {"region": 3}):
            assert post("/api/warnings", bad)[0] == 400
        assert post("/api/warnings", {"region": "yh"}) == (200, {"region": "yh", "chosen": "yh"}) and json.loads(conf.read_text())["warnings_region"] == "yh"
        assert post("/api/warnings", {"region": ""})[1] == {"region": "ne", "chosen": ""}
    finally:
        httpd.shutdown()
        httpd.server_close()
