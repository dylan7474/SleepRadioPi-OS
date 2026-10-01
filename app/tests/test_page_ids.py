"""The web page: every element id is used once (a repeated id makes
getElementById find the wrong element -- Browse once took over the buttons panel's list)."""

import re
from collections import Counter
from pathlib import Path

PAGE = Path(__file__).parent.parent / "sleepradiopi" / "web" / "desktop.html"


def test_the_desktop_page_too() -> None:
    page = PAGE.with_name("desktop.html").read_text()
    ids = Counter(re.findall(r'\bid="([^"]+)"', page))
    assert [k for k, n in ids.items() if n > 1] == []
    assert "<title>" in page and "/api/status" in page


def test_the_pages_scripts_parse() -> None:
    """A stray bracket stops a whole page's script (the desktop went blank once)."""
    import shutil
    import subprocess
    deno = shutil.which("deno")
    if deno is None:
        import pytest
        pytest.skip("no deno here to parse the scripts with")
    for name in ("desktop.html", "remote.html"):
        js = "\n".join(re.findall(r"<script>(.*?)</script>", PAGE.with_name(name).read_text(), re.S))
        r = subprocess.run([deno, "eval", "new Function(await new Response(Deno.stdin.readable).text())"],
                           input=js, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{name}: {r.stderr[-400:]}"


def test_the_remote_too() -> None:
    page = PAGE.with_name("remote.html").read_text()
    ids = Counter(re.findall(r'\bid="([^"]+)"', page))
    assert [k for k, n in ids.items() if n > 1] == []
    # every $("id") it uses is on the page
    used = set(re.findall(r'\$\("([A-Za-z0-9]+)"\)', page))
    assert used <= set(ids), used - set(ids)
    assert "fonts.googleapis" not in page                       # (it must work on the hotspot, offline)


def test_slash_is_the_remote_and_classic_goes_to_the_desktop(tmp_path) -> None:
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from sleepradiopi.web.server import make_handler
    from test_ondemand import _od_station
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_od_station(tmp_path), None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        remote = urllib.request.urlopen(base + "/").read().decode()
        classic = urllib.request.urlopen(base + "/classic")
        landed, page = classic.geturl(), classic.read().decode()
    finally:
        httpd.shutdown()
    assert 'id="remote"' in remote and 'id="setup"' in remote
    assert landed.endswith("/desktop") and "/api/status" in page                 # (the classic page is retired)
