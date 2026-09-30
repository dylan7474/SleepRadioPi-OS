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
