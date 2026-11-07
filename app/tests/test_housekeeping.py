"""Leftover temp files, files not rewritten when nothing changed, and the pages sent compressed and kept."""
import gzip
import os
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi.config import atomic
from sleepradiopi.playback import audiobooks
from sleepradiopi.web import server
from sleepradiopi.web.server import make_handler


def test_old_temp_files_from_a_write_that_died_are_swept_and_nothing_else(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    old = tmp_path / "books.json.4ctm00pl.tmp"
    empty = tmp_path / "books.json.hrodm_gt.tmp"
    fresh = tmp_path / "books.json.AbCdEf12.tmp"          # (a write going on now)
    keep = [tmp_path / "books.json", tmp_path / "notes.tmp", tmp_path / "x.y.tmp", tmp_path / "stations.tsv"]
    for p in (old, empty, fresh, *keep):
        p.write_text("x")
    for p in (old, empty, *keep):
        os.utime(p, (now - 3600, now - 3600))
    os.utime(fresh, (now - 5, now - 5))
    assert sorted(atomic.sweep_tmp(tmp_path, now=now)) == sorted([old.name, empty.name])
    assert not old.exists() and not empty.exists() and fresh.exists() and all(p.exists() for p in keep)
    assert atomic.sweep_tmp(tmp_path / "nowhere", now=now) == []


def test_the_audiobook_cache_is_not_rewritten_when_a_scan_finds_the_same(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "books"
    root.mkdir()
    (root / "A Story.mp3").write_bytes(b"not really sound")
    monkeypatch.setattr(audiobooks, "_probe", lambda p: (60.0, [], {"title": "A Story", "author": "Someone", "album": "A Story"}))
    writes = []
    real = audiobooks.write_atomic
    monkeypatch.setattr(audiobooks, "write_atomic", lambda p, text: (writes.append(p), real(p, text)))
    cache = tmp_path / "cache" / "books.json"
    first = audiobooks.BookLibrary(root, cache)
    first.scan()
    assert len(writes) == 1
    first.scan()
    assert len(writes) == 1                                   # the same figures: no write
    again = audiobooks.BookLibrary(root, cache)               # the radio started again
    again.scan()
    assert len(writes) == 1
    (root / "Another.mp3").write_bytes(b"more")
    again.scan()
    assert len(writes) == 2                                   # a book came: written


def test_the_pages_are_sent_compressed_and_not_again_when_unchanged(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def get(path, **headers):
            req = urllib.request.Request(url + path, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()
        for path, page in (("/desktop", server.DESKTOP), ("/", server.REMOTE)):
            code, h, body = get(path, **{"Accept-Encoding": "gzip"})
            assert code == 200 and h["Content-Encoding"] == "gzip" and gzip.decompress(body) == page
            assert len(body) < len(page) / 2 and h["Vary"] == "Accept-Encoding" and h["Cache-Control"] == "no-cache"
            code, h2, body = get(path, **{"Accept-Encoding": "identity"})
            assert code == 200 and "Content-Encoding" not in h2 and body == page      # (a browser that doesn't take it)
            code, h3, body = get(path, **{"If-None-Match": h["ETag"], "Accept-Encoding": "gzip"})
            assert code == 304 and body == b"" and h3["ETag"] == h["ETag"]              # unchanged: nothing sent
            code, _, body = get(path, **{"If-None-Match": '"something-old"', "Accept-Encoding": "gzip"})
            assert code == 200 and gzip.decompress(body) == page                        # changed: the new page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_quick_runs_pick_the_test_files_for_what_changed() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("tests_for_changes", Path(__file__).parent.parent / "scripts" / "tests_for_changes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    got = mod.tests_for(["sleepradiopi/playback/conditions.py"])
    assert "tests/test_conditions.py" in got and "tests/test_quakes.py" in got      # (the by-name one, and one that imports it)
    assert "tests/test_page_ids.py" in mod.tests_for(["sleepradiopi/web/desktop.html"])
    assert mod.tests_for(["tests/test_health.py"]) == ["tests/test_health.py"]
    web = mod.tests_for(["sleepradiopi/web/routes_outside.py"])                                 # (an area of the web handler: the tests that name its API paths)
    assert "tests/test_health.py" in web and "tests/test_lightning.py" in web and len(web) < 20
    assert mod.tests_for(["README.md", "sleepradiopi/__init__.py"]) == []           # (docs and the package marker: nothing to run)
