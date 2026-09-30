"""The web page: every element id is used once (a repeated id makes
getElementById find the wrong element -- Browse once took over the buttons panel's list)."""

import re
from collections import Counter
from pathlib import Path

PAGE = Path(__file__).parent.parent / "sleepradiopi" / "web" / "page.html"


def test_no_id_is_used_twice() -> None:
    ids = Counter(re.findall(r'\bid="([^"]+)"', PAGE.read_text()))
    assert [k for k, n in ids.items() if n > 1] == []


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
    for name in ("page.html", "desktop.html"):
        js = "\n".join(re.findall(r"<script>(.*?)</script>", PAGE.with_name(name).read_text(), re.S))
        r = subprocess.run([deno, "eval", "new Function(await new Response(Deno.stdin.readable).text())"],
                           input=js, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{name}: {r.stderr[-400:]}"
