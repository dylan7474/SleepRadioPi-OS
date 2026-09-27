import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.config.power import ENV, can_power_off, request_power_off
from sleepradiopi.web.server import make_handler


class FakeStation:
    def status(self):
        return {"on_air": False}


class FakeSpeaker:
    def __init__(self):
        self.saved = self.paused = False

    def save_now(self):
        self.saved = True

    def pause(self):
        self.paused = True

    def status(self):
        return {"volume": 50, "playing": not self.paused}


@pytest.fixture
def server():
    speaker = FakeSpeaker()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, speaker))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", speaker
    httpd.shutdown()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def _post(url):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_no_shutdown_without_the_variable(monkeypatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert not can_power_off()
    assert not request_power_off()


def test_no_shutdown_if_the_request_folder_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV, str(tmp_path / "missing" / "poweroff"))
    assert not can_power_off()
    assert not request_power_off()


def test_request_creates_the_file(tmp_path: Path, monkeypatch) -> None:
    flag = tmp_path / "poweroff"
    monkeypatch.setenv(ENV, str(flag))
    assert can_power_off()
    assert request_power_off()
    assert flag.exists()


def test_page_hides_shutdown_when_not_offered(server, monkeypatch) -> None:
    url, speaker = server
    monkeypatch.delenv(ENV, raising=False)
    assert _get(url + "/api/status")["can_power_off"] is False
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(url + "/api/power")
    assert e.value.code == 404
    assert not speaker.paused


def test_shutdown_from_the_page(server, tmp_path: Path, monkeypatch) -> None:
    url, speaker = server
    flag = tmp_path / "poweroff"
    monkeypatch.setenv(ENV, str(flag))
    assert _get(url + "/api/status")["can_power_off"] is True
    assert _post(url + "/api/power") == {"shutting_down": True}
    assert flag.exists()
    assert speaker.saved and speaker.paused    # volume kept, speaker silent at once
