import base64
import json
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi import remote
from sleepradiopi.config.auth import Auth
from sleepradiopi.web.server import make_handler

TOKEN = base64.b64encode(json.dumps({"a": "acct", "t": "tunnel-id", "s": "c2VjcmV0"}).encode()).decode()


class FakeStation:
    def status(self):
        return {"on_air": False}


@pytest.fixture
def remote_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "data" / "cloudflared"
    d.parent.mkdir()                                   # (/data exists on the radio)
    binary = tmp_path / "cloudflared"
    binary.write_text("")
    monkeypatch.setenv(remote.ENV, str(d))
    monkeypatch.setattr(remote, "BINARY", binary)
    monkeypatch.setattr(remote, "STATE_FILE", tmp_path / "remote-status")
    monkeypatch.setattr(remote, "_connected", lambda: False)
    return d


def test_not_available_without_the_variable_or_the_program(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(remote.ENV, raising=False)
    assert remote.status() == {"available": False}
    monkeypatch.setenv(remote.ENV, str(tmp_path / "cloudflared-dir"))
    monkeypatch.setattr(remote, "BINARY", tmp_path / "missing")
    assert not remote.available()


def test_token_is_checked_and_a_pasted_command_is_understood() -> None:
    assert remote.parse_token(TOKEN) == TOKEN
    assert remote.parse_token(f"  sudo cloudflared service install {TOKEN}\n") == TOKEN
    for bad in ("", "   ", "eyJhbGciOi", "not base64 at all!", base64.b64encode(b"{}").decode(),
                base64.b64encode(b'{"a": 1, "t": "x", "s": "y"}').decode(), None, 5, "x" * 3000):
        with pytest.raises(ValueError):
            remote.parse_token(bad)


def test_off_by_default_and_needs_a_token_first(remote_dir: Path) -> None:
    s = remote.status()
    assert s["available"] and not s["enabled"] and not s["token_set"] and s["state"] == "off"
    with pytest.raises(ValueError):
        remote.set_enabled(True)
    assert not (remote_dir / "enabled").exists()


def test_token_is_private_and_never_comes_back(remote_dir: Path) -> None:
    remote.set_token(TOKEN)
    f = remote_dir / "token"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert f.read_text().strip() == TOKEN
    assert TOKEN not in json.dumps(remote.status())
    remote.set_enabled(True)
    assert remote.status()["enabled"]
    remote.set_enabled(False)
    assert not (remote_dir / "enabled").exists() and f.exists()
    remote.set_enabled(True)
    remote.forget()
    assert not f.exists() and not (remote_dir / "enabled").exists()


def test_state_comes_from_the_helper_only_while_on(remote_dir: Path) -> None:
    remote.set_token(TOKEN)
    remote.STATE_FILE.write_text("running\n")
    assert remote.status()["state"] == "off"          # (stale words from before it was switched off)
    remote.set_enabled(True)
    assert remote.status()["state"] == "running"
    remote.STATE_FILE.write_text("waiting for the clock\n")
    assert remote.status()["state"] == "waiting for the clock" and not remote.status()["connected"]


def _server(conf: Path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _call(url: str, body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_the_api(tmp_path: Path, remote_dir: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd, base = _server(conf)
    try:
        assert _call(base + "/api/remote")[1]["token_set"] is False
        assert _call(base + "/api/remote", {"enabled": True})[0] == 400             # no token yet
        assert _call(base + "/api/remote", {"token": "nonsense"})[0] == 400
        code, st = _call(base + "/api/remote", {"token": TOKEN})
        assert code == 200 and st["token_set"] and TOKEN not in json.dumps(st)
        assert _call(base + "/api/remote", {"enabled": "yes"})[0] == 400
        assert _call(base + "/api/remote", {"enabled": True})[1]["enabled"] is True
        assert _call(base + "/api/remote", {"enabled": False})[1]["enabled"] is False
        _, st = _call(base + "/api/remote", {"forget": True})
        assert not st["token_set"] and not st["enabled"]
        assert TOKEN not in conf.read_text()
    finally:
        httpd.shutdown()


def test_no_api_where_there_is_no_remote_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(remote.ENV, raising=False)
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd, base = _server(conf)
    try:
        assert _call(base + "/api/remote") == (200, {"available": False})
        assert _call(base + "/api/remote", {"enabled": True})[0] == 404
    finally:
        httpd.shutdown()


def test_the_password_still_applies_through_the_tunnel(tmp_path: Path, remote_dir: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    Auth(conf).set_password("hunter22")
    httpd, base = _server(conf)
    try:
        assert _call(base + "/api/status")[0] == 200                               # the radio itself: no password
        for h in ({"Cf-Connecting-Ip": "203.0.113.9"}, {"Cf-Ray": "abc-LHR"}, {"X-Forwarded-For": "203.0.113.9"}):
            assert _call(base + "/api/status", headers=h)[0] == 401               # via the tunnel: login needed
            assert _call(base + "/api/remote", {"enabled": False}, headers=h)[0] == 401
    finally:
        httpd.shutdown()
