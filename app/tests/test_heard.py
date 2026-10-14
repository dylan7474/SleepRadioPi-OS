"""Who has been heard, by every reader (audio/heard.py)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import ft8, heard
from sleepradiopi.web.server import make_handler


class Clock:
    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def test_a_call_after_de_read_twice_is_heard() -> None:
    clock = Clock(1000.0)
    s = heard.Store(clock)
    s.where = {"band": "40 m", "dial": 7_020_000, "sideband": "usb"}
    t = heard.Text("cw", s)
    assert t.take(23, "CQ CQ DE G4ABC ", 690.0) == []                  # once: not yet believed
    got = t.take(23, "G4ABC K ", 690.0)                                 # (the same over goes on: not after DE, so it doesn't count)
    assert got == []
    got = t.take(23, "CQ DE G4ABC G4ABC K ", 690.0)                     # twice
    assert [(h["call"], h["band"], h["family"], h["cq"], h["hz"], h["count"]) for h in got] == [("G4ABC", "40 m", "cw", True, 7_020_690, 1)]
    clock.at += 60
    got = t.take(23, "G3XYZ DE G4ABC UR 599 ", 690.0)                   # believed now: each sighting counts; G3XYZ is being called, not heard
    assert [(h["call"], h["count"], h["cq"]) for h in got] == [("G4ABC", 2, False)] and s.get("40 m", "G3XYZ", "cw") is None


def test_text_comes_in_pieces_and_in_lower_case() -> None:
    s = heard.Store(Clock(0.0))
    s.where = {"band": "20 m", "dial": 14_069_000, "sideband": "usb"}
    t = heard.Text("psk31", s)
    for piece in ("cq cq de ea", "1abc ea1abc pse k\\n", "cq cq de ea1a", "bc ea1abc pse k "):     # (split in the middle of the call)
        t.take(("psk31", 60), piece, 1200.0)
    h = s.get("20 m", "EA1ABC", "psk31")
    assert h is not None and h["count"] == 1 and h["hz"] == 14_070_200 and h["family"] == "psk"
    t.take(("psk31", 60), "de ea1abc loc in83mm ", 1200.0)              # its locator, after its call
    assert s.get("20 m", "EA1ABC", "psk31")["grid"] == "IN83mm"
    t.take(("psk31", 60), "the end of my call de ea1ab", 1200.0)       # (a call with nothing after it yet: not taken)
    assert s.get("20 m", "EA1AB", "psk31") is None


def test_not_a_call_and_not_twice_in_time() -> None:
    clock = Clock(0.0)
    s = heard.Store(clock)
    t = heard.Text("rtty", s)
    for text in ("DE 599 ", "DE TEST ", "DE IO94 ", "DE JO31 "):          # no call there
        t.take(1, text)
        t.take(1, text)
    assert s.list() == []
    t.take(1, "DE G4ABC ")
    clock.at += heard.TWICE_S + 1                                       # the second reading too long after the first
    t.take(1, "DE G4ABC ")
    assert s.list() == []
    t.take(1, "DE G4ABC ")
    assert [h["call"] for h in s.list()] == ["G4ABC"] and s.list()[0]["band"] is None   # (the page hadn't said where: no band)


def test_families_kept_apart_and_forgotten_apart(tmp_path: Path) -> None:
    clock = Clock(100.0)
    s = heard.Store(clock)
    s.add("ft8", "20 m", "K1ABC", 100.0, -5.0, "FN42", True, 14_075_000)
    s.add("ft4", "20 m", "K1ABC", 110.0, 3.0)                          # FT4 is FT8's family: the same line
    s.add("cw", "20 m", "K1ABC", 120.0)                                # Morse: another
    k = s.get("20 m", "K1ABC", "ft8")
    assert (k["count"], k["snr"], k["grid"], k["mode"], k["family"]) == (2, 3.0, "FN42", "ft4", "wsjt")
    assert [(h["family"], h["call"]) for h in s.list()] == [("cw", "K1ABC"), ("wsjt", "K1ABC")] and len(s.list({"wsjt"})) == 1
    s.forget({"cw"})
    assert [h["family"] for h in s.list()] == ["wsjt"]
    with pytest.raises(ValueError):
        s.add("olivia", "20 m", "K1ABC")


def test_ft8s_list_as_it_was_kept_is_read_back(tmp_path: Path) -> None:
    file = tmp_path / "heard.json"
    file.write_text(json.dumps({"heard": [{"band": "40 m", "call": "DL1ABC", "grid": "JO31", "snr": 1.0, "first": 10, "last": 20, "count": 2, "cq": False}]}))
    s = heard.Store(Clock(30.0))
    s.keep_in(file)
    k = s.get("40 m", "DL1ABC", "ft8")
    assert (k["family"], k["mode"], k["count"], k["grid"]) == ("wsjt", "ft8", 2, "JO31")
    s.add("psk31", "40 m", "DL1ABC", 30.0)
    s.save()
    again = heard.Store(Clock(40.0))
    again.keep_in(file)
    assert sorted(h["family"] for h in again.list()) == ["psk", "wsjt"]


def test_where_on_the_air() -> None:
    assert heard.on_air({}, 700.0) is None and heard.on_air({"dial": 7_020_000}, None) is None
    assert heard.on_air({"dial": 7_020_000, "sideband": "usb"}, 700.0) == 7_020_700
    assert heard.on_air({"dial": 7_020_000, "sideband": "lsb"}, 700.0) == 7_019_300
    assert heard.on_air({"dial": 7_020_000, "sideband": "cw", "centre": 700}, 750.0) == 7_020_050


def test_the_page_asks_for_it_and_says_where(tmp_path: Path, monkeypatch) -> None:
    s = heard.Store()
    monkeypatch.setattr(heard, "store", s)
    monkeypatch.setattr(ft8, "HOME", "IO94")
    s.add("ft8", "20 m", "K1ABC", None, -5.0, "FN42")
    s.add("cw", "40 m", "G4ABC")
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, d = call("/api/rx/heard")
        assert code == 200 and d["home"] == "IO94" and [(h["family"], h["call"]) for h in d["heard"]] == [("cw", "G4ABC"), ("wsjt", "K1ABC")]
        assert 5000 < d["heard"][1]["km"] < 5200                        # (how far, as FT8's always were)
        assert call("/api/rx/where", {"band": "40 m", "dial": 7_020_000, "sideband": "cw", "centre": 700}) == \
            (200, {"where": {"band": "40 m", "dial": 7_020_000, "sideband": "cw", "centre": 700}})
        assert s.where["dial"] == 7_020_000
        for bad in ({"dial": "7020"}, {"band": 40}, {"sideband": "u s b"}, {"centre": 9000}):
            assert call("/api/rx/where", bad)[0] == 400
        assert call("/api/rx/heard", {"clear": True, "families": ["cw"]})[1]["heard"][0]["call"] == "K1ABC"
        assert call("/api/rx/heard", {"clear": True, "families": ["olivia"]})[0] == 400
        assert call("/api/rx/heard", {"clear": True})[1]["heard"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
