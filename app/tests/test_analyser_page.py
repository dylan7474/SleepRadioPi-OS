import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi import wifi
from sleepradiopi.config.auth import Auth
from sleepradiopi.web.server import make_handler


class FakeStation:
    def status(self):
        return {"on_air": False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.mark.parametrize("hotspot", [False, True])
def test_the_radio_serves_the_analyser(tmp_path: Path, monkeypatch, hotspot) -> None:
    """For tuning with no internet (e.g. on the hotspot): no password needed, and
    not sent to the main page like the hotspot's other addresses."""
    if hotspot:
        monkeypatch.setattr(wifi, "status", lambda run=None: {"mode": "hotspot"})
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    Auth(conf).set_password("secret1")
    handler = make_handler(FakeStation(), None, None, conf)
    handler._local = lambda self: False                      # another device on the network
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(base + "/analyser/") as r:
            page = r.read().decode()
        assert r.status == 200 and "micHelp" in page and "Insecure origins treated as secure" in page
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(base + "/analyser")
        assert err.value.code == 301 and err.value.headers["Location"] == "/analyser/"
        with pytest.raises(urllib.error.HTTPError) as err:        # the rest still needs the password
            opener.open(base + "/api/status")
        assert err.value.code == 401
    finally:
        httpd.shutdown()
