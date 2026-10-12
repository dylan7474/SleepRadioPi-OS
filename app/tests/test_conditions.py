"""Band conditions, from what the radio has heard, and the sun's figures (playback/conditions.py)."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import ft8
from sleepradiopi.playback import conditions, survey
from sleepradiopi.web.server import make_handler

NOW = 1_800_000_000.0


def test_which_part_of_the_world_a_locator_is_in() -> None:
    where = {"IO94": "EU", "JO31": "EU", "JN70": "EU", "KP20": "EU", "KO85": "EU", "IM76": "EU", "HP94": "EU", "FN42": "NA", "EN37": "NA", "CM87": "NA",
             "GG66": "SA", "FF46": "SA", "KG33": "AF", "JJ00": "AF", "PM95": "AS", "OL72": "AS", "NK94": "AS", "QF56": "OC", "RF73": "OC"}
    assert {g: conditions.region(g) for g in where} == where
    assert conditions.region("xx") is None and conditions.region("") is None and conditions.region(None) is None
    assert conditions.regions(["IO91", "JO31", "FN42", "xx", "PM95", "JN18"]) == {"EU": 3, "NA": 1, "AS": 1}


def test_a_band_said_plainly() -> None:
    say = lambda *a, **k: conditions.band_says("20 m", *a, **k)
    b = say(16, 5516, {"EU": 10, "NA": 5, "AS": 1})
    assert (b["state"], b["to"], b["n"], b["km"]) == ("good", ["Europe", "North America"], 16, 5516)      # (one station in sixteen isn't "open to Asia")
    assert b["says"] == "20 m is good: open to Europe and North America, 16 stations, the farthest 5,516 km."
    assert say(7, 602, {"EU": 7})["says"] == "20 m is fair: open, but only round about (7 stations, none past 602 km)."
    assert say(1, 9200, {"AS": 1})["says"] == "20 m is poor: open to Asia, 1 station, the farthest 9,200 km."
    assert say(0, None, None) == {"band": "20 m", "state": "nothing heard", "n": 0, "to": [], "says": "20 m: nothing heard."}
    assert say(6, None, None)["says"] == "20 m is fair, 6 stations."                       # (no locator set on the radio: no distances)
    assert say(12, 3000, {"EU": 9, "NA": 2, "AF": 1})["to"] == ["Europe", "North America"]
    # against a while ago: only when it's a real change
    assert say(16, 5516, {"EU": 16}, before=5)["says"].endswith("Busier than it was.") and say(5, 2000, {"EU": 5}, before=14)["says"].endswith("Quieter than it was.")
    assert say(16, 5516, {"EU": 16}, before=12)["says"].endswith("km.")
    assert say(4, 2000, {"EU": 4}, before=0)["says"].endswith("It has opened.") and say(0, None, None, before=6)["says"] == "20 m: nothing heard. It has closed."
    assert say(1, 2000, {"EU": 1}, before=0)["says"].endswith("km.")                         # (one station isn't an opening)
    assert "10 stations in the last hour" in say(3, 5100, {"EU": 9, "NA": 1}, hour=10)["says"]


def _found() -> dict:
    return {"ft8:160 m": {"n": 0, "t": NOW - 600, "day": [[NOW - 4200, 0], [NOW - 600, 0]]},
            "ft8:40 m": {"n": 7, "km": 602, "to": {"EU": 7}, "t": NOW - 500, "day": [[NOW - 4100, 2], [NOW - 500, 7]]},
            "ft8:20 m": {"n": 16, "km": 5516, "to": {"EU": 10, "NA": 5, "AS": 1}, "t": NOW - 400, "day": [[NOW - 4000, 15], [NOW - 400, 16]]},
            "ft8:15 m": {"n": 4, "km": 9200, "to": {"AS": 2, "EU": 2}, "t": NOW - 300, "day": [[NOW - 3900, 0], [NOW - 300, 4]]},
            "ft8:10 m": {"n": 0, "t": NOW - 200, "day": [[NOW - 3800, 5], [NOW - 200, 0]]},
            "ft4:20 m": {"n": 5, "t": NOW}, "cw:20 m": {"n": 2, "t": NOW, "where": []}, "ft8:80 m": {"n": 9, "t": NOW - 3 * 3600}}


def test_how_the_bands_are_from_a_survey() -> None:
    s = conditions.summary(_found(), [], NOW)
    assert [b["band"] for b in s["bands"]] == ["160 m", "40 m", "20 m", "15 m", "10 m"]      # FT8 only, in the bands' order; a survey three hours old says nothing now
    by = {b["band"]: b for b in s["bands"]}
    assert by["20 m"]["says"] == "20 m is good: open to Europe and North America, 16 stations, the farthest 5,516 km." and by["20 m"]["by"] == "survey"
    assert by["15 m"]["says"].endswith("It has opened.") and by["10 m"]["says"] == "10 m: nothing heard. It has closed." and by["40 m"]["says"].endswith("Busier than it was.")
    assert by["20 m"]["day"] == _found()["ft8:20 m"]["day"] and by["20 m"]["t"] == NOW - 400
    assert s["says"] == "20 m is the busiest, open to Europe and North America. The longest reach is on 15 m: 9,200 km. Nothing on 160 m and 10 m." and s["t"] == NOW - 200
    assert conditions.summary({}, [], NOW) == {"bands": [], "says": "Nothing to go by yet: a survey of FT8 on every band says how the bands are.", "t": None}
    dead = conditions.summary({"ft8:10 m": {"n": 0, "t": NOW}, "ft8:12 m": {"n": 0, "t": NOW}}, [], NOW)
    assert dead["says"] == "Nothing heard on any band surveyed."


def test_and_from_who_has_been_heard_where_theres_no_survey() -> None:
    heard = [{"band": "30 m", "call": f"DL{i}ABC", "grid": "JO31", "last": NOW - 100 * i, "km": 700} for i in range(9)]
    heard += [{"band": "30 m", "call": "K1ABC", "grid": "FN42", "last": NOW - 50, "km": 5100}, {"band": "17 m", "call": "OLD", "grid": "JO31", "last": NOW - 7200},
              {"band": None, "call": "X", "last": NOW}, {"band": "20 m", "call": "G4XYZ", "grid": "IO91", "last": NOW - 30, "km": 350}]
    s = conditions.summary(_found(), heard, NOW)
    by = {b["band"]: b for b in s["bands"]}
    assert by["30 m"]["by"] == "heard" and by["30 m"]["heard"] == 10 and by["30 m"]["says"] == "30 m is poor: open to Europe and North America, 10 stations in the last hour, the farthest 5,100 km."
    assert "17 m" not in by                          # (heard two hours ago: not how it is now)
    assert by["20 m"]["by"] == "survey"              # a fresh survey of the band is the better word than a list being added to
    late = conditions.summary({"ft8:20 m": {"n": 2, "t": NOW - 5000}}, heard, NOW)
    assert {b["band"]: b["by"] for b in late["bands"]} == {"30 m": "heard", "20 m": "heard"}      # ...unless it's older than what's been heard since


def test_the_survey_says_which_parts_of_the_world(monkeypatch) -> None:
    monkeypatch.setattr(ft8, "HOME", "IO94")
    read = [{"snr": 3.0, "dt": 0.5, "hz": 1000, "text": "CQ K1ABC FN42"}, {"snr": -5.0, "dt": 0.4, "hz": 1500, "text": "G4XYZ DL1ABC JO31"},
            {"snr": 0.0, "dt": 0.5, "hz": 1200, "text": "CQ F5ABC JN18"}, {"snr": 1.0, "dt": 0.5, "hz": 700, "text": "TNX BOB 73 GL"}]
    x = np.zeros(int(16 * survey.AUDIO_RATE), np.float32)
    got = survey.slot_heard(x, 0.0, 0.0, "ft8", lambda samples, mode: [dict(m) for m in read])
    assert got["n"] == 4 and got["to"] == {"NA": 1, "EU": 2} and 5000 < got["km"] < 5200
    monkeypatch.setattr(ft8, "HOME", None)           # (no locator on the radio: where they are is still known, how far isn't)
    got = survey.slot_heard(x, 0.0, 0.0, "ft8", lambda samples, mode: [dict(m) for m in read])
    assert got == {"n": 4, "to": {"NA": 1, "EU": 2}}
    assert survey.slot_heard(x, 0.0, 0.0, "ft8", lambda samples, mode: []) == {"n": 0}


def test_the_suns_figures_said() -> None:
    assert conditions.sun_says(122, 0.67) == "Solar flux 122: middling: 20 and 17 m by day, 15 m some days. K index 0.67: the earth's field is quiet."
    assert "the sun is busy" in conditions.sun_says(180, 2) and "the sun is quiet" in conditions.sun_says(72, 1)
    assert "unsettled" in conditions.sun_says(None, 4.33) and conditions.sun_says(None, 4.33).startswith("K index")
    stormy = conditions.sun_says(140, 6, {"R": {"Scale": "2"}, "S": {"Scale": "0"}, "G": {"Scale": "3"}})
    assert "a magnetic storm" in stormy and "a radio blackout on the sunlit side at R2" in stormy and "a geomagnetic storm at G3" in stormy
    assert conditions.sun_says(None, None) == ""


def test_noaas_figures_are_fetched_now_and_then_and_kept(tmp_path: Path) -> None:
    clock, asked = [NOW], []
    answers = {conditions.K_URL: [{"time_tag": "2026-10-12T09:00:00", "Kp": 1.33, "a_running": 5}, {"time_tag": "2026-10-12T12:00:00", "Kp": 0.67, "a_running": 3}],
               conditions.FLUX_URL: [{"flux": 122, "time_tag": "2026-10-11T20:00:00"}], conditions.SCALES_URL: {"0": {"R": {"Scale": "0", "Text": "none"}, "G": {"Scale": "1"}}}}

    def get(url):
        asked.append(url)
        if answers.get(url) is None:
            raise urllib.error.URLError("no route")
        return answers[url]

    sun = conditions.Sun(get=get, clock=lambda: clock[0])
    sun.keep_in(tmp_path / "cache" / "sun.json")
    assert sun.status() is None                      # nothing yet: asked for, in a thread of its own
    for _ in range(100):
        if sun._had is not None and not sun._busy:
            break
        time.sleep(0.02)
    st = sun.status()
    assert (st["flux"], st["k"], st["a"], st["scales"]["G"]) == (122.0, 0.67, 3, {"Scale": "1"}) and "K index 0.67" in st["says"] and "G1" in st["says"]
    n = len(asked)
    clock[0] += conditions.SUN_EVERY_S - 60
    assert sun.status()["flux"] == 122.0 and len(asked) == n       # not asked again so soon
    again = conditions.Sun(get=get, clock=lambda: clock[0])      # the radio started again: what it last had is there
    again.keep_in(tmp_path / "cache" / "sun.json")
    assert again.status()["k"] == 0.67
    answers[conditions.K_URL] = None                 # no internet: what it had goes on being said, until it's too old to be how the sun is
    clock[0] += 3600
    assert sun.fetch() is None and sun.status()["flux"] == 122.0
    clock[0] += conditions.SUN_OLD_S
    assert sun.status() is None
    answers[conditions.K_URL] = [["time_tag", "Kp", "a_running", "station_count"], ["2026-10-12 12:00:00.000", "3.00", "15", "8"]]      # (as NOAA used to lay it out)
    answers[conditions.FLUX_URL] = {"Flux": "150", "TimeStamp": "x"}
    assert sun.fetch()["k"] == 3.0 and sun.fetch()["flux"] == 150.0
    answers[conditions.SCALES_URL] = None            # (the scales alone not had: the rest still is)
    assert "scales" not in sun.fetch()


def test_as_the_dj_says_it() -> None:
    s = conditions.summary(_found(), [], NOW)
    assert conditions.spoken(s) == "The short wave bands. 20 metres is good, open to Europe and North America. 40 metres is fair, but only round about. Also open: 15 metres."
    said = conditions.spoken(s, {"flux": 122.0, "k": 0.67})
    assert said.endswith("Also open: 15 metres. The sun is middling and the earth's field is quiet.")
    assert conditions.spoken(s, {"flux": 180.0, "k": 6.0}).endswith("The sun is busy and there's a magnetic storm.")
    dead = conditions.summary({"ft8:10 m": {"n": 0, "t": NOW}}, [], NOW)
    assert conditions.spoken(dead) == "The short wave bands. Nothing heard on any band just now."
    assert conditions.spoken(conditions.summary({}, [], NOW)) is None             # nothing to go by: nothing said
    assert "km" not in said and " m " not in said and "," not in said.replace(", open", "").replace(", but", "")      # (words a voice can say)


class _Surveys:
    """What the survey has found, a receiver at a time."""

    def __init__(self, found: dict) -> None:
        self.all = found

    def bases(self) -> list[str]:
        return list(self.all)

    def found(self, base: str) -> dict:
        return self.all.get(base, {})


def test_the_dj_says_it_only_when_asked_and_once_in_its_hour(monkeypatch) -> None:
    from datetime import datetime
    at = lambda h, m: datetime.fromtimestamp(NOW).replace(hour=h, minute=m, second=0)
    fresh = lambda h, m: {"ft8:20 m": {"n": 16, "km": 5516, "to": {"EU": 10, "NA": 5}, "t": at(h, m).timestamp() - 300}}
    monkeypatch.setattr(conditions, "sun", conditions.Sun(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    monkeypatch.setattr(conditions, "_said", None)
    job = _Surveys({"http://old:1": {"ft8:40 m": {"n": 3, "t": at(9, 0).timestamp() - 9000}}, "http://receiver:8075": fresh(9, 20)})
    assert conditions.freshest(job)[0] == "http://receiver:8075"
    monkeypatch.setattr(conditions, "EVERY_H", 0)                # not asked for: never
    assert conditions.say_due(at(9, 20), True, False, job, []) is None
    monkeypatch.setattr(conditions, "EVERY_H", 3)                # every three hours: at 0, 3, 6, 9...
    assert conditions.say_due(at(9, 5), True, False, job, []) is None             # not before a quarter past (the news, the survey still going)
    assert conditions.say_due(at(9, 20), False, False, job, []) is None           # the clock isn't to be trusted
    assert conditions.say_due(at(9, 20), True, True, job, []) is None             # the news's quiet hours
    words = conditions.say_due(at(9, 20), True, False, job, [])
    assert words == "The short wave bands. 20 metres is good, open to Europe and North America."
    assert conditions.say_due(at(9, 40), True, False, job, []) is None            # once in the hour
    job.all["http://receiver:8075"] = fresh(10, 20)
    assert conditions.say_due(at(10, 20), True, False, job, []) is None           # ten o'clock isn't one of its hours
    job.all["http://receiver:8075"] = {"ft8:20 m": {"n": 16, "t": at(12, 20).timestamp() - 3 * 3600}}
    assert conditions.say_due(at(12, 20), True, False, job, []) is None           # nothing recent to go by: nothing said...
    job.all["http://receiver:8075"] = fresh(12, 40)
    assert conditions.say_due(at(12, 40), True, False, job, []) is not None       # ...until there is, later in the hour


def test_the_station_puts_it_in_a_gap_like_a_message(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime
    from test_offline import _station
    st = _station(tmp_path / "st")
    monkeypatch.setattr(conditions, "EVERY_H", 0)
    assert st._bands_due(datetime.fromtimestamp(NOW)) is None    # not asked for
    monkeypatch.setattr(conditions, "EVERY_H", 1)
    asked = []
    monkeypatch.setattr(conditions, "say_due", lambda at, clock_ok, quiet, job, heard: asked.append((clock_ok, quiet, type(heard))) or "The short wave bands. 20 metres is good.")
    st.config.news_quiet_hours, st.config.news_quiet_start_min, st.config.news_quiet_end_min = True, 23 * 60, 6 * 60
    noon, night = datetime.fromtimestamp(NOW).replace(hour=12, minute=20), datetime.fromtimestamp(NOW).replace(hour=2, minute=20)
    assert st._bands_due(noon) == "The short wave bands. 20 metres is good." and asked[-1][1:] == (False, list)
    st._bands_due(night)
    assert asked[-1][1] is True                      # (the small hours: the news's quiet hours are its own)
    monkeypatch.setattr(conditions, "say_due", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert st._bands_due(noon) is None               # nothing of the bands may stop the show


def test_conditions_from_the_web(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    now = time.time()
    job = survey.Job()
    job._found = {"http://receiver:8075": {"ft8:20 m": {"n": 16, "km": 5516, "to": {"EU": 10, "NA": 5}, "t": round(now - 60), "day": [[round(now - 60), 16]]}}}
    monkeypatch.setattr(survey, "job", job)
    monkeypatch.setattr(conditions, "sun", conditions.Sun(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(ft8.reader, "_heard", {("40 m", "DL1ABC"): {"band": "40 m", "call": "DL1ABC", "grid": "JO31", "snr": 1.0, "first": now - 90, "last": now - 90, "count": 2, "cq": False}})
    try:
        with urllib.request.urlopen(base + "/api/rx/conditions?base=http://receiver:8075/", timeout=5) as r:
            d = json.loads(r.read())
        assert [b["band"] for b in d["bands"]] == ["40 m", "20 m"] and d["bands"][1]["says"].startswith("20 m is good: open to Europe and North America")
        assert d["bands"][0]["by"] == "heard" and d["sun"] is None and d["home"] == "IO94" and d["says"].startswith("20 m is the busiest")
        assert d["base"] == "http://receiver:8075" and d["say_every_h"] == 0 and d["spoken"].startswith("The short wave bands. 20 metres is good, open to Europe and North America.")
        with urllib.request.urlopen(base + "/api/rx/conditions", timeout=5) as r:      # no receiver named: the one surveyed latest
            d = json.loads(r.read())
        assert [b["band"] for b in d["bands"]] == ["40 m", "20 m"] and d["base"] == "http://receiver:8075"

        def post(body):
            req = urllib.request.Request(base + "/api/rx/conditions", data=json.dumps(body).encode(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        monkeypatch.setattr(conditions, "EVERY_H", 0)                                  # how often the DJ says it: kept
        for bad in ({}, {"say_every_h": 4}, {"say_every_h": True}, {"say_every_h": "1"}):
            assert post(bad)[0] == 400 and conditions.EVERY_H == 0
        assert post({"say_every_h": 2}) == (200, {"say_every_h": 2}) and conditions.EVERY_H == 2 and json.loads(conf.read_text())["conditions_every_h"] == 2
        assert post({"say_every_h": 0}) == (200, {"say_every_h": 0}) and conditions.EVERY_H == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
