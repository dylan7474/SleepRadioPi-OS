import json
import random
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pytest

from sleepradiopi.broadcast.birthdays import (MAX_WISHES, BirthdayWishes, ordinal_words, todays,
                                              validate, wish_text)
from sleepradiopi.broadcast.news import QuietHours
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station


def test_ordinals() -> None:
    assert [ordinal_words(n) for n in (1, 2, 3, 11, 12, 20, 21, 40, 42, 99, 100, 101)] == [
        "first", "second", "third", "eleventh", "twelfth", "twentieth", "twenty-first",
        "fortieth", "forty-second", "ninety-ninth", "hundredth", "hundred and first"]


def test_validate_cleans_and_sorts() -> None:
    got = validate([{"name": "  Tom  Smith ", "day": 2, "month": 12},
                    {"name": "Sarah", "day": 14, "month": 3, "year": 1985},
                    {"name": "Leap", "day": 29, "month": 2}])
    assert [e["name"] for e in got] == ["Leap", "Sarah", "Tom Smith"]
    assert got[1] == {"name": "Sarah", "day": 14, "month": 3, "year": 1985}


@pytest.mark.parametrize("bad", [
    {}, [{"name": "", "day": 1, "month": 1}], [{"name": "A", "day": 31, "month": 4}],
    [{"name": "A", "day": 30, "month": 2}], [{"name": "A", "day": 1, "month": 13}],
    [{"name": "A", "day": "1", "month": 1}], [{"name": "A", "day": 1, "month": 1, "year": 1800}],
    [{"name": "A", "day": 29, "month": 2, "year": 2001}], [{"name": "x" * 61, "day": 1, "month": 1}],
])
def test_validate_rejects(bad) -> None:
    with pytest.raises(ValueError):
        validate(bad)


def test_whose_birthday_today_with_ages_and_leap_days() -> None:
    entries = validate([{"name": "Sarah", "day": 14, "month": 3, "year": 1985},
                        {"name": "Tom", "day": 14, "month": 3},
                        {"name": "Leap", "day": 29, "month": 2, "year": 2000}])
    assert todays(entries, date(2025, 3, 14)) == [{"name": "Sarah", "age": 40}, {"name": "Tom", "age": None}]
    assert todays(entries, date(2025, 2, 28)) == [{"name": "Leap", "age": 25}]   # not a leap year
    assert todays(entries, date(2028, 2, 28)) == []
    assert todays(entries, date(2028, 2, 29)) == [{"name": "Leap", "age": 28}]


def test_wish_wording() -> None:
    text = wish_text([{"name": "Sarah", "age": 40}, {"name": "Tom", "age": None}], "Beatles Radio",
                     random.Random(1))
    assert "happy fortieth birthday to Sarah and happy birthday to Tom" in text
    assert text[0].isupper()
    for seed in range(20):   # every template reads properly
        t = wish_text([{"name": "Ann", "age": None}], "Sleep Radio", random.Random(seed))
        assert "Ann" in t and "{" not in t


def test_wishes_are_spaced_capped_and_quiet_at_night() -> None:
    w = BirthdayWishes([{"name": "Sarah", "day": 14, "month": 3}], QuietHours.of_minutes(23 * 60, 6 * 60))
    day = lambda h, m=0: datetime(2025, 3, 14, h, m)
    assert w.due(day(2), True) == []                         # quiet hours
    assert w.due(day(8), False) == []                        # clock not trusted
    assert w.due(day(8), True) == [{"name": "Sarah", "age": None}]
    w.wished(day(8))
    assert w.due(day(9), True) == []                         # 90 min apart
    times = [day(9, 30), day(11), day(12, 30), day(14), day(15, 30)]
    n = 1
    for t in times:
        if w.due(t, True):
            w.wished(t); n += 1
    assert n == MAX_WISHES
    assert w.due(datetime(2025, 3, 15, 9), True) == []       # not their day


def test_the_station_puts_the_wish_first_in_the_gap(tmp_path: Path, monkeypatch) -> None:
    today = date.today()
    st = _station(tmp_path, birthdays=[{"name": "Sarah", "day": today.day, "month": today.month}])
    st.birthdays.quiet = None
    monkeypatch.setattr("sleepradiopi.broadcast.station.clock_trusted", lambda: True)
    said = []
    monkeypatch.setattr(st, "_say", lambda text, *a: said.append(text) or text)
    track = st._take_next()
    plan = st._plan_gap(track, st._take_next())
    assert plan[0].kind == "say" and "birthday to Sarah" in plan[0].speech   # (_say returns the text here)
    assert len(plan) >= 2                                                   # the usual link still follows
    said.clear()
    st._plan_gap(track, st._take_next())
    assert not any("birthday" in t for t in said)            # not again straight away


def test_bad_birthdays_in_the_config_dont_stop_the_station(tmp_path: Path) -> None:
    st = _station(tmp_path, birthdays=[{"name": "", "day": 40, "month": 1}])
    assert st.birthdays.entries == []


def test_backup_checks_birthdays() -> None:
    good = {"format": backup.FORMAT, "version": 1, "settings": {"birthdays": [
        {"name": "Sarah", "day": 14, "month": 3}]}}
    assert backup.parse(good)[0]["birthdays"][0]["name"] == "Sarah"
    with pytest.raises(backup.BadSettings):
        backup.parse({**good, "settings": {"birthdays": [{"name": "A", "day": 31, "month": 2}]}})


def test_web_api(tmp_path: Path) -> None:
    st = _station(tmp_path)
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"speaker_mono": True}))
    httpd = __import__("http.server").server.ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(st, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        assert req("/api/birthdays")["birthdays"] == []                     # blank by default
        got = req("/api/birthdays", {"birthdays": [{"name": "Tom", "day": 2, "month": 12},
                                                   {"name": "Sarah", "day": 14, "month": 3}]})
        assert [b["name"] for b in got["birthdays"]] == ["Sarah", "Tom"]
        assert [b["name"] for b in load(conf).birthdays] == ["Sarah", "Tom"]
        assert json.loads(conf.read_text())["speaker_mono"] is True        # the rest kept
        with pytest.raises(urllib.error.HTTPError) as err:
            req("/api/birthdays", {"birthdays": [{"name": "X", "day": 31, "month": 4}]})
        assert err.value.code == 400 and "isn't a date" in json.load(err.value)["error"]
        with pytest.raises(urllib.error.HTTPError) as err:                  # no speaker here
            req("/api/birthdays/hear", {"name": "Sarah", "day": 14, "month": 3})
        assert err.value.code == 400
    finally:
        httpd.shutdown()
