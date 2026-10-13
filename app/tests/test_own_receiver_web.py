"""A receiver of your own, asked through the radio (/api/rx/own/...): so the page asks only the radio (no network)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.web.server import make_handler


class _Receiver(BaseHTTPRequestHandler):
    """A receiver service, near enough: it says what it was asked."""
    asked: list = []

    def _say(self, code: int, d: dict) -> None:
        body = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.asked.append(("GET", self.path, None))
        if self.path == "/bands":
            self._say(200, {"2m": {"name": "2 m", "channels": []}})
        elif self.path.startswith("/spectrum"):
            self._say(200, {"asked": self.path})
        else:
            self._say(200, {"receiving": {"band": "2m"}})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.asked.append(("POST", self.path, body))
        if body.get("freq") == 0:
            self._say(400, {"error": "freq is a frequency"})
        else:
            self._say(200, {"tuned": body})

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def both(tmp_path: Path):
    """A receiver, and a radio with a station saved that plays from it -> (the radio's address, the receiver's, call)."""
    _Receiver.asked = []
    rx = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    origin = f"http://127.0.0.1:{rx.server_address[1]}"
    conf = tmp_path / "config.toml"
    conf.write_text(json.dumps({"radio_stations": [{"name": "2 m", "url": f"{origin}/audio?band=2m", "info": ""},
                                                   {"name": "A stream", "url": "http://stream.example/live.mp3", "info": ""}]}))
    radio = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    for httpd in (rx, radio):
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{radio.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        yield origin, call, rx
    finally:
        for httpd in (rx, radio):
            httpd.shutdown()
            httpd.server_close()


def test_the_receiver_is_asked_by_the_radio_and_its_answer_passed_back(both) -> None:
    origin, call, _ = both
    assert call(f"/api/rx/own/bands?base={origin}") == (200, {"2m": {"name": "2 m", "channels": []}})
    assert call(f"/api/rx/own/status?base={origin}/") == (200, {"receiving": {"band": "2m"}})
    assert call(f"/api/rx/own/spectrum?base={origin}&since=41") == (200, {"asked": "/spectrum?since=41"})   # (the rest of the question goes with it)
    assert call(f"/api/rx/own/tune?base={origin}", {"freq": 145500000, "mode": "nfm"}) == (200, {"tuned": {"freq": 145500000, "mode": "nfm"}})
    assert call(f"/api/rx/own/hold?base={origin}", {"freq": None}) == (200, {"tuned": {"freq": None}})
    assert _Receiver.asked == [("GET", "/bands", None), ("GET", "/status", None), ("GET", "/spectrum?since=41", None),
                               ("POST", "/tune", {"freq": 145500000, "mode": "nfm"}), ("POST", "/hold", {"freq": None})]


def test_the_receivers_own_no_comes_back_with_its_reason(both) -> None:
    origin, call, _ = both
    assert call(f"/api/rx/own/tune?base={origin}", {"freq": 0}) == (400, {"error": "freq is a frequency"})


def test_only_a_receiver_of_your_own_and_only_what_the_page_asks(both) -> None:
    origin, call, _ = both
    for path, body in ((f"/api/rx/own/bands?base=http://stream.example", None),         # a saved station, but no receiver
                       (f"/api/rx/own/bands?base=http://127.0.0.1:1", None),             # nobody's
                       ("/api/rx/own/bands", None),
                       (f"/api/rx/own/audio?base={origin}", None),                       # not asked of one by the page
                       (f"/api/rx/own/calibrate?base={origin}", {"ppm": 1, "trim": 0}),
                       (f"/api/rx/own/tune?base={origin}", None),                        # said, not asked
                       (f"/api/rx/own/status?base={origin}", {}),
                       (f"/api/rx/own/../status?base={origin}", None)):
        code, said = call(path, body)
        assert code == 400 and "error" in said, path
    assert _Receiver.asked == []


def test_a_receiver_that_isnt_answering_says_so(both) -> None:
    origin, call, rx = both
    rx.shutdown()
    rx.server_close()
    code, said = call(f"/api/rx/own/bands?base={origin}")
    assert code == 502 and said["error"].startswith("the receiver isn't answering")
