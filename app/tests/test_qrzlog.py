"""The owner's QRZ logbook (playback/qrzlog.py): QRZ's answers read, the key kept to itself."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import ft8
from sleepradiopi.playback import conditions, qrzlog, room
from sleepradiopi.web.server import make_handler

NOW = 1_792_340_000.0
KEY = "ABCD-0A0B-1C1D-2E2F"
ADIF = ("<call:6>DL1ABC<band:3>20m<mode:3>FT8<qso_date:8>20261018<time_on:6>141000<gridsquare:4>JO31<country:27>Federal Republic of Germany<eor>"
        "<call:6>VK2XYZ<band:3>15m<mode:3>FT8<qso_date:8>20261017<time_on:4>0930<gridsquare:6>QF56od<comment:3>R&R<eor>")
STATUS = urllib.parse.urlencode({"RESULT": "OK", "DATA": "BOOK_NAME=M8ODJ&QSO_COUNT=1204&CONFIRMED=388&DXCC_COUNT=141&START_DATE=2024-03-01"})


def _qrz(answers: dict, asked: list):
    def post(fields):
        asked.append(fields)
        a = answers.get(fields["ACTION"])
        if a is None:
            raise urllib.error.URLError("no route")
        return a
    return post


@pytest.fixture(autouse=True)
def _set_up(monkeypatch):
    monkeypatch.setattr(ft8, "HOME", "IO94")
    monkeypatch.setattr(room, "CALLSIGN", "M8ODJ")
    monkeypatch.setattr(qrzlog, "KEY", "")
    monkeypatch.setattr(conditions, "_alerted", {})
    monkeypatch.setattr(conditions, "ALERTS", False)


def test_qrzs_answers_read() -> None:
    a = qrzlog.answer(STATUS)
    assert (a["RESULT"], a["QSO_COUNT"], a["DXCC_COUNT"], a["BOOK_NAME"]) == ("OK", "1204", "141", "M8ODJ")
    plain = qrzlog.answer("RESULT=OK&COUNT=2&ADIF=" + ADIF)                                        # (the ADIF as it is, & and all)
    assert plain["COUNT"] == "2" and plain["ADIF"] == ADIF
    escaped = qrzlog.answer("RESULT=OK&COUNT=2&ADIF=" + ADIF.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    assert escaped["ADIF"] == ADIF
    encoded = qrzlog.answer(urllib.parse.urlencode({"RESULT": "OK", "ADIF": ADIF, "COUNT": "2"}))
    assert encoded["ADIF"] == ADIF and encoded["COUNT"] == "2"
    assert qrzlog.answer("STATUS=AUTH&RESULT=AUTH&REASON=invalid api key&EXTENDED=")["REASON"] == "invalid api key"
    rs = qrzlog.adif(ADIF)
    assert [r["call"] for r in rs] == ["DL1ABC", "VK2XYZ"] and rs[1]["comment"] == "R&R" and rs[0]["country"] == "Federal Republic of Germany"
    q = qrzlog.qso(rs[0])
    assert (q["call"], q["band"], q["mode"], q["country"]) == ("DL1ABC", "20m", "FT8", "Federal Republic Of Germany") and 500 < q["km"] < 700
    assert qrzlog.qso({"call": "x", "qso_date": "junk"})["t"] is None
    assert qrzlog.pick({"QSO_COUNT": "1,204"}, qrzlog.TOTAL) == 1204 and qrzlog.pick({}, qrzlog.TOTAL) is None


def test_the_logbook_fetched_kept_and_forgotten(tmp_path: Path) -> None:
    asked, key = [], [KEY]
    answers = {"STATUS": STATUS, "FETCH": "RESULT=OK&COUNT=2&ADIF=" + ADIF}
    lb = qrzlog.Logbook(post=_qrz(answers, asked), clock=lambda: NOW, key=lambda: key[0])
    lb.keep_in(tmp_path / "qrz.json")
    had = lb.fetch()
    assert (had["total"], had["confirmed"], had["dxcc"]) == (1204, 388, 141) and [q["call"] for q in had["recent"]] == ["DL1ABC", "VK2XYZ"]
    assert [a["ACTION"] for a in asked] == ["STATUS", "FETCH"] and asked[1]["OPTION"] == "MODSINCE:2025-10-18,AFTERLOGID:0,MAX:250,TYPE:ADIF"
    assert all(a["KEY"] == KEY for a in asked) and had["fetched"] == {"result": "OK", "count": "2", "reason": None, "since": "2025-10-18"}
    assert KEY not in (tmp_path / "qrz.json").read_text()                       # (the book kept, never the key)
    assert lb.status()["says"] == "1,204 QSOs in the logbook, 388 confirmed, 141 countries, the latest DL1ABC on 20m (FT8)."
    t = qrzlog.tile(had, set())[0]
    assert (t["value"], t["sub"], t["state"]) == ("1,204", "141 countries · last DL1ABC 20m", "good")
    answers["FETCH"] = None                                                     # the latest not had: the figures still are
    again = lb.fetch()
    assert again["recent"] == [] and again["fetched"]["result"] == "unreachable"
    answers["FETCH"] = "RESULT=FAIL&REASON=invalid option&COUNT=0"
    assert lb.fetch()["fetched"] == {"result": "FAIL", "count": "0", "reason": "invalid option", "since": "2025-10-18"}
    key[0] = ""
    assert lb.status() is None                                                  # no key: nothing shown
    lb.forget()
    assert not (tmp_path / "qrz.json").exists()


def test_qrz_saying_no() -> None:
    lb = qrzlog.Logbook(post=_qrz({"STATUS": "STATUS=AUTH&RESULT=AUTH&REASON=subscription required&EXTENDED="}, []), clock=lambda: NOW, key=lambda: KEY)
    assert lb.fetch() is None and lb.refused == "subscription required"
    assert qrzlog.Logbook(post=_qrz({}, []), key=lambda: "").fetch() is None    # no key: QRZ not asked


def test_a_new_country_said_once(monkeypatch) -> None:
    clock = [NOW]
    answers = {"STATUS": STATUS, "FETCH": "RESULT=OK&COUNT=0"}
    lb = qrzlog.Logbook(post=_qrz(answers, []), clock=lambda: clock[0], key=lambda: KEY)
    lb.fetch()
    assert qrzlog.alerts(lb.status(), NOW) == []                                 # (the first look: nothing's new)
    answers["STATUS"] = STATUS.replace("DXCC_COUNT%3D141", "DXCC_COUNT%3D142")
    clock[0] += 1800
    had = lb.fetch()
    assert qrzlog.alerts(had, clock[0]) == [("qrz:dxcc:142", 1, "A new country in your QRZ logbook: 142 now.", "New country: 142 in the logbook")]
    assert qrzlog.alerts(had, clock[0] + qrzlog.NEW_FRESH_S + 1) == []           # (not news hours later)
    monkeypatch.setattr(qrzlog, "logbook", lb)
    monkeypatch.setattr(conditions, "ALERTS", True)
    for name in ("sun", "forecast", "aurora"):
        monkeypatch.setattr(conditions, name, type(getattr(conditions, name))(get=lambda url: (_ for _ in ()).throw(OSError("offline"))))
    from datetime import datetime
    from sleepradiopi.playback import quakes, ukwarnings
    monkeypatch.setattr(quakes, "quakes", quakes.Quakes(get_json=lambda u: (_ for _ in ()).throw(OSError("x")), get_xml=lambda u: (_ for _ in ()).throw(OSError("x"))))
    monkeypatch.setattr(ukwarnings, "warnings", ukwarnings.Warnings(get=lambda u: (_ for _ in ()).throw(OSError("x"))))
    monkeypatch.setattr(qrzlog.time, "time", lambda: clock[0])
    at = datetime.fromtimestamp(clock[0])
    assert conditions.alert_due(at, True, False, clock[0]) == "A new country in your QRZ logbook: 142 now."
    assert conditions.alert_due(at, True, False, clock[0] + 60) is None


def test_the_key_from_the_web_and_never_back(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    asked = []
    monkeypatch.setattr(qrzlog, "logbook", qrzlog.Logbook(post=_qrz({"STATUS": STATUS, "FETCH": "RESULT=OK&COUNT=2&ADIF=" + ADIF}, asked)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/qrz", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.read().decode()
    try:
        assert json.loads(get("/api/qrz"))["set"] is False and not asked              # no key: QRZ isn't asked
        for bad in ({}, {"key": 3}, {"key": "has spaces in it"}, {"key": "x" * 65}):
            assert post(bad)[0] == 400
        status, body = post({"key": KEY})
        d = json.loads(body)
        assert status == 200 and d["set"] and d["total"] == 1204 and d["dxcc"] == 141 and [q["call"] for q in d["recent"]] == ["DL1ABC", "VK2XYZ"]
        assert KEY not in body and KEY not in get("/api/qrz") and KEY not in get("/api/outside")    # never sent back
        assert json.loads(conf.read_text())["qrz_key"] == KEY and qrzlog.KEY == KEY
        assert "qrz" in [t["key"] for t in json.loads(get("/api/outside"))["tiles"]]
        from sleepradiopi.config import backup
        assert "qrz_key" in backup.LOCAL                                            # (never in a backup file)
        status, body = post({"key": ""})
        assert status == 200 and json.loads(body)["set"] is False and json.loads(body)["had"] is False and json.loads(conf.read_text())["qrz_key"] == ""
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_big_book_a_page_at_a_time(monkeypatch) -> None:
    monkeypatch.setattr(qrzlog, "PAGE", 2)
    pages, asked = {0: ADIF.replace("<eor>", "<app_qrzlog_logid:2>10<eor>", 1).replace("15m<mode", "15m<app_qrzlog_logid:2>11<mode"),
                    12: "<call:5>G4NEW<band:3>40m<mode:2>CW<qso_date:8>20261018<time_on:4>2000<app_qrzlog_logid:2>12<eor>"}, []

    def post(fields):
        asked.append(fields)
        if fields["ACTION"] == "STATUS":
            return STATUS
        after = int(fields["OPTION"].split("AFTERLOGID:")[1].split(",")[0])
        return "RESULT=OK&COUNT=3&ADIF=" + pages[after]
    had = qrzlog.Logbook(post=post, clock=lambda: NOW, key=lambda: KEY).fetch()
    assert [q["call"] for q in had["recent"]] == ["G4NEW", "DL1ABC", "VK2XYZ"]           # (all pages, the latest first)
    assert [a["OPTION"].split(",")[1] for a in asked[1:]] == ["AFTERLOGID:0", "AFTERLOGID:12"]


def test_the_tile_is_there_with_no_key(monkeypatch) -> None:
    assert [(t["value"], t["sub"]) for t in qrzlog.tile(None, set())] == [("key?", "add your access key")]
    monkeypatch.setattr(qrzlog, "KEY", KEY)
    assert qrzlog.tile(None, set())[0]["sub"] == "asking QRZ"
    monkeypatch.setattr(qrzlog.logbook, "refused", "subscription required")
    assert qrzlog.tile(None, set())[0]["says"] == "QRZ said no: subscription required."


def test_who_it_says_it_is() -> None:
    agent = qrzlog._agent()
    assert agent.startswith("SleepRadio/") and agent.endswith("(M8ODJ)") and len(agent) <= 128
