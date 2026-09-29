import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast.messages import Messages, date_line, validate
from sleepradiopi.config import backup
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station

HOME = "You're at Willow Court, and you're safe here."
SUNDAY = "Dylan is coming to see you on Sunday."


def test_settings_are_checked_and_filled_in() -> None:
    cfg = validate({"list": [{"text": "  Hello   there "}]})
    assert cfg["every_min"] == 15 and cfg["offset_min"] == 7 and cfg["on"] and cfg["date_first"]
    assert cfg["list"] == [{"text": "Hello there"}]
    assert validate({"list": [{"text": "x", "until": "2026-10-04"}]})["list"][0]["until"] == "2026-10-04"
    for bad in ({"every_min": 7}, {"offset_min": 15}, {"start_min": 2000}, {"list": [{"text": ""}]},
                {"list": [{"text": "x", "until": "Sunday"}]}, {"list": [{"text": "x" * 401}]}, {"on": "yes"},
                {"list": "hello"}):
        with pytest.raises(ValueError):
            validate(bad)


def test_the_day_and_date() -> None:
    assert date_line(date(2026, 9, 29)) == "It's Tuesday, the twenty-ninth of September."
    assert date_line(date(2026, 10, 1)) == "It's Thursday, the first of October."


def test_slots_are_seven_past_and_every_quarter_hour() -> None:
    m = Messages({"list": [{"text": HOME}]})
    assert m.slot(datetime(2026, 9, 29, 10, 7)) == datetime(2026, 9, 29, 10, 7)
    assert m.slot(datetime(2026, 9, 29, 10, 21, 59)) == datetime(2026, 9, 29, 10, 7)
    assert m.slot(datetime(2026, 9, 29, 10, 53)) == datetime(2026, 9, 29, 10, 52)
    assert m.slot(datetime(2026, 9, 29, 10, 3)) == datetime(2026, 9, 29, 9, 52)
    hourly = Messages({"every_min": 60, "offset_min": 7, "list": [{"text": HOME}]})
    assert hourly.slot(datetime(2026, 9, 29, 10, 50)) == datetime(2026, 9, 29, 10, 7)


def test_one_message_per_slot_taking_turns() -> None:
    m = Messages({"list": [{"text": HOME}, {"text": SUNDAY}], "date_first": False})
    t = datetime(2026, 9, 29, 10, 8)
    assert m.due(t, True) == HOME
    m.played(t)
    assert m.due(datetime(2026, 9, 29, 10, 15), True) is None          # that slot's done
    assert m.due(datetime(2026, 9, 29, 10, 23), True) == SUNDAY        # the next one: the next message
    m.played(datetime(2026, 9, 29, 10, 23))
    assert m.due(datetime(2026, 9, 29, 10, 40), True) == HOME
    assert m.due(datetime(2026, 9, 29, 10, 40), False) is None         # the clock isn't set


def test_only_in_the_day_and_until_the_last_day() -> None:
    m = Messages({"list": [{"text": SUNDAY, "until": "2026-10-04"}], "date_first": False})
    assert m.due(datetime(2026, 9, 29, 7, 52), True) is None           # before 8 am
    assert m.due(datetime(2026, 9, 29, 21, 7), True) is None           # after 9 pm
    assert m.due(datetime(2026, 10, 4, 20, 55), True) == SUNDAY        # the last day
    assert m.due(datetime(2026, 10, 5, 12, 0), True) is None           # then no more
    m.set({**m.cfg, "list": [{"text": SUNDAY, "off": True}]})
    assert m.due(datetime(2026, 9, 29, 12, 0), True) is None           # paused
    assert Messages({"on": False, "list": [{"text": HOME}]}).due(datetime(2026, 9, 29, 12, 0), True) is None


def test_the_date_comes_first() -> None:
    m = Messages({"list": [{"text": HOME}]})
    assert m.due(datetime(2026, 9, 29, 12, 0), True) == f"It's Tuesday, the twenty-ninth of September. {HOME}"


def test_next_slot_for_the_page() -> None:
    m = Messages({"list": [{"text": HOME}]})
    assert m.next_slot(datetime(2026, 9, 29, 10, 10)) == datetime(2026, 9, 29, 10, 7)   # still to play
    m.played(datetime(2026, 9, 29, 10, 10))
    assert m.next_slot(datetime(2026, 9, 29, 10, 10)) == datetime(2026, 9, 29, 10, 22)
    assert m.next_slot(datetime(2026, 9, 29, 22, 0)) == datetime(2026, 9, 30, 8, 7)    # tomorrow
    assert Messages().next_slot(datetime(2026, 9, 29, 10, 0)) is None


class Said(str):
    """A stand-in for a Speech: its text, and a future that's never needed."""
    @property
    def text(self):
        return str(self)

    future = type("F", (), {"cancel": lambda self: None, "done": lambda self: True})()


def test_the_station_reads_a_message_in_the_gap(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path, messages={"list": [{"text": HOME}], "start_min": 0, "end_min": 24 * 60})
    monkeypatch.setattr("sleepradiopi.broadcast.station.clock_trusted", lambda: True)
    monkeypatch.setattr(st, "_say", lambda text, *a: Said(text))
    track = st._take_next()
    st._plan = st._plan_gap(track, st._take_next())
    assert not any(s.speech.text.endswith(HOME) for s in st._plan if s.kind == "say")
    st._prefetch_gap(st._plan, datetime.now())                # near the end of the track
    assert st._plan[0].kind == "say" and st._plan[0].speech.text.endswith(HOME)
    assert len(st._plan) >= 2                                 # the usual link still follows
    st._replan_gap(st._take_next())                           # a request re-words the gap: it's kept
    assert st._plan[0].speech.text.endswith(HOME)
    st._prefetch_gap(st._plan, datetime.now(), again=True)    # (a skip: not added twice)
    assert sum(s.kind == "say" and s.speech.text.endswith(HOME) for s in st._plan) == 1
    st._plan = st._plan_gap(track, st._take_next())           # the next gap, same slot: none
    st._prefetch_gap(st._plan, datetime.now())
    assert not any(s.kind == "say" and s.speech.text.endswith(HOME) for s in st._plan)


def test_bad_messages_in_the_config_dont_stop_the_station(tmp_path: Path) -> None:
    st = _station(tmp_path, messages={"every_min": 7})
    assert st.messages.cfg["list"] == []


def test_backup_checks_messages() -> None:
    good = {"format": backup.FORMAT, "version": 1, "settings": {"messages": {"list": [{"text": HOME}]}}}
    assert backup.parse(good)[0]["messages"]["list"][0]["text"] == HOME
    with pytest.raises(backup.BadSettings):
        backup.parse({**good, "settings": {"messages": {"every_min": 7}}})


def test_the_messages_api(tmp_path: Path) -> None:
    st = _station(tmp_path)
    st.tts = None
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, d = call("/api/messages")
        assert code == 200 and d["list"] == [] and d["every_min"] == 15 and d["next"] is None
        code, d = call("/api/messages", {**d, "list": [{"text": HOME}]})
        assert code == 200 and d["list"] == [{"text": HOME}] and d["next"]
        assert json.loads(conf.read_text())["messages"]["list"] == [{"text": HOME}]
        assert st.messages.cfg["list"] == [{"text": HOME}]
        code, d = call("/api/messages", {"every_min": 7})
        assert code == 400 and "every_min" in d["error"]
        code, d = call("/api/messages/hear", {"text": HOME})
        assert code == 400                                   # no speaker here
    finally:
        httpd.shutdown()


def test_a_message_waits_when_the_news_is_due(tmp_path: Path, monkeypatch) -> None:
    st = _station(tmp_path, news_enabled=True,
                  messages={"list": [{"text": HOME}], "start_min": 0, "end_min": 24 * 60})
    monkeypatch.setattr("sleepradiopi.broadcast.station.clock_trusted", lambda: True)
    monkeypatch.setattr(st, "_say", lambda text, *a: Said(text))
    track = st._take_next()
    st._plan = st._plan_gap(track, st._take_next())
    st._news_ready = type("N", (), {"time_line": Said("x"), "due": None})()
    monkeypatch.setattr(st.news_schedule, "due_at", lambda at: object())
    st._maybe_add_message(datetime.now())
    assert not any(s.kind == "say" and s.speech.text.endswith(HOME) for s in st._plan)
    st._news_ready = None                                     # read: the message goes in the next gap
    st._maybe_add_message(datetime.now())
    assert st._plan[0].speech.text.endswith(HOME)
