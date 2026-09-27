import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.config import auth as auth_mod
from sleepradiopi.config import backup
from sleepradiopi.config.auth import COOKIE, Auth, main
from sleepradiopi.web.server import make_handler


def test_no_password_by_default(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    a = Auth(conf)
    assert not a.protected and a.valid(None) and not a.check("x")


def test_password_hash_token_and_logout_on_change(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"news_enabled": True}))
    a = Auth(conf)
    a.set_password("letmein")
    saved = json.loads(conf.read_text())
    assert saved["news_enabled"] is True and "letmein" not in conf.read_text()
    assert a.protected and a.check("letmein") and not a.check("wrong")
    t = a.token()
    assert a.valid(t) and not a.valid(None) and not a.valid("junk")
    expiry, sig = t.split(".")
    assert not a.valid(f"{int(expiry) + 10}.{sig}")            # tampered expiry
    monkeypatch.setattr(auth_mod.time, "time", lambda: int(expiry) + 1)
    assert not a.valid(t)                                       # expired
    monkeypatch.undo()
    time.sleep(0.01)
    a.set_password("newpass")
    assert not a.valid(t)                                       # everyone logged out
    with pytest.raises(ValueError):
        a.set_password("abc")                                   # too short


def test_changes_on_disk_are_seen_at_once(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    a = Auth(conf)
    other = Auth(conf)                                          # e.g. the ssh command
    other.set_password("secret1")
    assert a.protected
    time.sleep(0.01)
    assert main(["--config", str(conf), "clear"]) == 0
    assert not a.protected


def test_the_password_never_goes_in_the_settings_file(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    Auth(conf).set_password("secret1")
    assert "web_password" not in json.dumps(backup.export(conf, 30))
    settings, _ = backup.parse({"format": backup.FORMAT, "version": 1,
                                "settings": {"web_password": {"salt": "00", "hash": "00"}}})
    assert settings == {}                                       # and can't be loaded from one


class FakeStation:
    def status(self):
        return {"on_air": False}


@pytest.fixture
def remote(tmp_path: Path):
    """A server that treats the test client as another device on the network."""
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    handler = make_handler(FakeStation(), None, None, conf)
    handler._local = lambda self: False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST", headers=headers)
        with urllib.request.urlopen(r) as resp:
            return json.load(resp) if resp.headers["Content-Type"] == "application/json" else None, \
                resp.headers.get("Set-Cookie")
    yield req, conf
    httpd.shutdown()


def test_web_login_flow(remote) -> None:
    req, conf = remote
    assert req("/api/status")[0]["on_air"] is False             # open without a password
    state, cookie = req("/api/password", {"password": "hunter22"})
    assert state["protected"] and COOKIE in cookie              # this browser stays logged in
    mine = cookie.split(";")[0]
    with pytest.raises(urllib.error.HTTPError) as err:
        req("/api/status")                                      # others need to log in
    assert err.value.code == 401
    assert req("/")[0] is None                                  # the page itself loads (its login form)
    assert req("/api/auth")[0] == {"protected": True, "logged_in": False, "local": False}
    with pytest.raises(urllib.error.HTTPError) as err:
        req("/api/login", {"password": "nope"})
    assert err.value.code == 401
    _, cookie2 = req("/api/login", {"password": "hunter22"})
    theirs = cookie2.split(";")[0]
    assert req("/api/status", cookie=theirs)[0]["on_air"] is False
    assert req("/api/status", cookie=mine)[0]["on_air"] is False
    _, cleared = req("/api/logout", {}, cookie=theirs)
    assert "Max-Age=0" in cleared
    req("/api/password", {"password": None}, cookie=mine)       # remove it: open again
    assert req("/api/status")[0]["on_air"] is False


def test_the_radio_itself_never_needs_the_password(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    Auth(conf).set_password("hunter22")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/status") as r:
            assert json.load(r)["on_air"] is False
    finally:
        httpd.shutdown()
