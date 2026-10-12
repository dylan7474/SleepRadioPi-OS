"""Messages from away: a document of the owner's that the radio fetches (broadcast/away.py)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast import away, messages

DOC = """# for Dad
You're at Willow Court, and you're safe here.

[at 10:00 16.00] [until 2026-10-18] Dylan is coming
to see you on Sunday.

[every 60] [from 09:00 to 18:00] [no date] Lunch is at half past twelve.

[days sat sun] [theme Carisbrooke] There's a film in the lounge tonight.

[off] Kept for later.
"""
LINK = "https://docs.google.com/document/d/1AbC_d-9/edit?usp=sharing"


def test_a_document_of_messages() -> None:
    items, wrong = away.parse(DOC)
    assert wrong == [] and [m["text"] for m in items] == [
        "You're at Willow Court, and you're safe here.", "Dylan is coming to see you on Sunday.", "Lunch is at half past twelve.",
        "There's a film in the lounge tonight.", "Kept for later."]
    assert all(m["away"] for m in items) and "station" not in items[0]          # (whichever station is playing)
    assert items[0] == {"text": "You're at Willow Court, and you're safe here.", "every": 15, "from": "08:00", "to": "21:00", "date_first": True, "away": True}
    assert items[1]["times"] == ["10:00", "16:00"] and items[1]["until"] == "2026-10-18"
    assert (items[2]["every"], items[2]["from"], items[2]["to"], items[2]["date_first"]) == (60, "09:00", "18:00", False)
    assert items[3]["days"] == [5, 6] and items[3]["station"] == "Carisbrooke" and items[4]["off"] is True
    # what isn't understood is left out and said, not read at the wrong times
    items, wrong = away.parse("[whenever] Tea is at four.\n\n[every 7] Too often.\n\n[at 25:00] No such time.\n\nThis one is fine.\r\n\r\n[at 09:00]")
    assert [m["text"] for m in items] == ["This one is fine."] and len(wrong) == 4 and "[whenever] isn't a note I know" in wrong[0]
    assert away.parse("") == ([], []) and away.parse("# nothing yet\n\n") == ([], [])         # an empty document: no messages
    # a sign-in page is not a document of messages (a link that isn't shared)
    with pytest.raises(ValueError, match="anyone with the link"):
        away.parse("<!DOCTYPE html><html><head><title>Sign in</title>")
    # JSON, as the radio's own list
    items, wrong = away.parse(json.dumps({"list": [{"text": "Hello.", "every": 30, "station": "Carisbrooke"}]}))
    assert items == [{"text": "Hello.", "every": 30, "from": "08:00", "to": "21:00", "date_first": True, "station": "Carisbrooke", "away": True}]
    with pytest.raises(ValueError):
        away.parse("\n\n".join(f"Message {i}." for i in range(messages.MAX_MESSAGES + 2)))


def test_the_links_people_copy() -> None:
    assert away.direct(LINK) == "https://docs.google.com/document/d/1AbC_d-9/export?format=txt"
    assert away.direct("https://drive.google.com/file/d/XYZ/view?usp=drive_link") == "https://drive.google.com/uc?export=download&id=XYZ"
    assert away.direct("https://www.dropbox.com/s/abc/notes.txt?dl=0") == "https://www.dropbox.com/s/abc/notes.txt?dl=1"
    assert away.direct("https://gist.github.com/dylan/0123abc") == "https://gist.githubusercontent.com/dylan/0123abc/raw"
    assert away.direct("https://example.org/m.txt") == "https://example.org/m.txt"
    assert away.validate({"url": "  " + LINK + " ", "every_min": 60}) == {"url": LINK, "every_min": 60}
    assert away.validate(None) == {"url": "", "every_min": 30}
    for bad in ({"url": "not a link"}, {"url": "ftp://x.y/z"}, {"url": LINK, "every_min": 5}, {"url": 3}, []):
        with pytest.raises(ValueError):
            away.validate(bad)


class _Web:
    def __init__(self, text=DOC):
        self.text, self.asked, self.now = text, [], 1_800_000_000.0

    def fetch(self, url):
        self.asked.append(url)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def test_fetched_kept_and_said_with_the_radios_own(tmp_path: Path) -> None:
    web, told = _Web(), []
    a = away.Away({"url": LINK, "every_min": 30}, fetch=web.fetch, clock=lambda: web.now, on_change=told.append)
    file = tmp_path / "state" / "messages-away.json"
    a.keep_in(file)
    assert a.due() and a.look()["count"] == 5 and web.asked == [away.direct(LINK)] and len(told) == 1
    st = a.status()
    assert st["t"] == web.now and "error" not in st and st["list"][0]["text"].startswith("You're at Willow Court")
    assert not a.due()
    web.now += 29 * 60
    assert not a.due()
    web.now += 2 * 60
    assert a.due()
    a.look()
    assert len(told) == 1                                   # nothing changed: nobody told
    # the Wi-Fi is down for a week: the same messages, said still; tried again in five minutes, not thirty
    web.text = OSError("no network")
    web.now += 31 * 60
    st = a.look()
    assert st["count"] == 5 and "couldn't be fetched" in st["error"] and len(told) == 1
    assert not a.due()
    web.now += away.RETRY_S + 1
    assert a.due()
    # the link stops being shared: said plainly, and the old ones kept
    web.text = urllib.error.HTTPError(LINK, 403, "Forbidden", {}, None)
    assert "anyone with the link" in a.look()["error"] and a.status()["count"] == 5
    web.text = "<html><body>Sign in"
    assert "anyone with the link" in a.look()["error"] and a.status()["count"] == 5
    # the radio starts again with no internet: what was kept is there, and the station is told
    told2 = []
    b = away.Away({"url": LINK, "every_min": 30}, fetch=web.fetch, clock=lambda: web.now, on_change=told2.append)
    b.keep_in(file)
    assert b.status()["count"] == 5 and len(told2) == 1 and "station" not in told2[0][0] and told2[0][3]["station"] == "Carisbrooke"
    assert LINK in file.read_text()
    # the document emptied: no messages from away; a different link: the old one's are forgotten at once
    web.text = "# nothing just now\n"
    web.now += away.RETRY_S + 1
    assert a.look()["count"] == 0 and told[-1] == []
    web.text = DOC
    a.look()
    a.set({"url": "https://example.org/other.txt", "every_min": 15})
    assert a.status()["count"] == 0 and told[-1] == [] and json.loads(file.read_text())["list"] == []
    c = away.Away({"url": LINK, "every_min": 30})
    c.keep_in(file)                                         # (kept for another link: not this one's)
    assert c.status()["count"] == 0
    a.set({"url": "", "every_min": 15})
    assert not a.due() and a.look()["count"] == 0 and web.asked[-1] != ""


def test_which_station_says_them() -> None:
    m = messages.Messages({"list": [{"text": "The radio's own.", "station": "Default"}]})
    items, _ = away.parse("Said anywhere.\n\n[theme Carisbrooke] Only there.")
    m.set_away(items)
    day = datetime(2026, 10, 12).date()
    assert [x["text"] for x in m.active(day, None)] == ["The radio's own.", "Said anywhere."]
    assert [x["text"] for x in m.active(day, "Carisbrooke")] == ["Said anywhere.", "Only there."]
    assert "Said anywhere." in m.due(datetime(2026, 10, 12, 9, 8), True, "Carisbrooke")
    assert m.next_slot(datetime(2026, 10, 12, 3, 0), "Carisbrooke") == datetime(2026, 10, 12, 8, 7)
    assert [x["text"] for x in m.cfg["list"]] == ["The radio's own."]            # (never mixed into what's saved)
    m.set_away([])
    assert m.active(day, "Carisbrooke") == []


def test_from_the_web_page(tmp_path: Path, monkeypatch) -> None:
    from sleepradiopi.web.server import make_handler

    class Doc(BaseHTTPRequestHandler):
        text = DOC

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = Doc.text.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    docs = ThreadingHTTPServer(("127.0.0.1", 0), Doc)
    threading.Thread(target=docs.serve_forever, daemon=True).start()
    link = f"http://127.0.0.1:{docs.server_address[1]}/messages.txt"

    class FakeStation:
        _has_voice = False
        profile = None
        messages = messages.Messages()

        def status(self):
            return {"on_air": False}
    remote = away.Away(on_change=FakeStation.messages.set_away)
    monkeypatch.setattr(away, "remote", remote)
    config = tmp_path / "config.json"
    config.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, None, config))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    try:
        assert call("/api/messages")[1]["away"] == {"url": "", "every_min": 30, "count": 0, "list": [], "busy": False}
        code, reply = call("/api/messages/away", {"url": "nonsense", "every_min": 30})
        assert code == 400 and "address" in reply["error"]
        code, reply = call("/api/messages/away", {"url": link, "every_min": 60})                 # saved, and fetched at once
        assert code == 200 and reply["away"]["count"] == 5 and reply["list"] == [] and "error" not in reply["away"]
        assert json.loads(config.read_text())["messages_away"] == {"url": link, "every_min": 60}
        assert len(FakeStation.messages.away) == 5
        Doc.text = "Only this now."
        code, reply = call("/api/messages/away/look", {})
        assert code == 200 and [m["text"] for m in reply["away"]["list"]] == ["Only this now."]
        code, reply = call("/api/messages/away", {"url": "", "every_min": 60})
        assert code == 200 and reply["away"]["count"] == 0 and FakeStation.messages.away == []
    finally:
        httpd.shutdown()
        docs.shutdown()
