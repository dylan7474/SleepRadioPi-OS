"""The Log window: reading the radio's log (logview.py, GET /api/log)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sleepradiopi import logview
from sleepradiopi.web.server import make_handler

LOG = """Nov  7 21:39:55 nightingale-radio auth.info sshd-session[19300]: Accepted publickey for root from 192.168.50.123 port 52594 ssh2
Nov  7 21:40:13 nightingale-radio user.notice sleepradiopi: 2026-11-07 21:40:13,897 INFO root: say: The radio has been updated to version 1.9.55.
Nov  7 21:40:14 nightingale-radio user.notice update: confirmed
Nov  7 21:40:19 nightingale-radio user.notice sleepradiopi: 2026-11-07 21:40:19,592 ERROR sleepradiopi.audio.speaker: aplay stopped; reopening the speaker (1 in a row)
Nov  7 21:40:21 nightingale-radio daemon.warn avahi-daemon[249]: WARNING: No NSS support for mDNS detected
Nov  7 21:41:00 nightingale-radio user.notice sleepradiopi: Process tts-worker:
Nov  7 21:41:00 nightingale-radio user.notice sleepradiopi: Traceback (most recent call last):
Nov  7 21:41:00 nightingale-radio user.notice sleepradiopi: ModuleNotFoundError: No module named 'sherpa_onnx'
Nov  7 21:41:30 nightingale-radio user.notice sleepradiopi: 2026-11-07 21:41:30,001 INFO sleepradiopi.web.server: 192.168.50.123 "GET /desktop HTTP/1.1" 200 -
Nov  7 21:42:00 nightingale-radio kern.err kernel: [ 100.0] mmc0: card trouble
"""


def _file(tmp_path: Path, text: str = LOG) -> Path:
    f = tmp_path / "messages"
    f.write_text(text)
    return f


def test_a_line_is_split_into_when_how_bad_who_and_what() -> None:
    p = logview.parse("Nov  7 21:40:19 nightingale-radio user.notice sleepradiopi: 2026-11-07 21:40:19,592 ERROR sleepradiopi.audio.speaker: aplay stopped; reopening")
    assert (p["t"], p["level"], p["who"], p["text"]) == ("Nov  7 21:40:19", "ERROR", "sleepradiopi.audio.speaker", "aplay stopped; reopening")
    s = logview.parse("Nov  7 21:39:55 nightingale-radio auth.info sshd-session[19300]: Accepted publickey for root")
    assert (s["level"], s["who"], s["text"]) == ("INFO", "sshd-session[19300]", "Accepted publickey for root")
    assert logview.parse("Nov  7 21:40:21 h daemon.warn avahi-daemon[249]: WARNING: x")["level"] == "WARNING"      # (syslog's own severity, for what isn't the radio's)
    assert logview.parse("Nov  7 21:42:00 h kern.err kernel: [ 100.0] mmc0: card trouble")["level"] == "ERROR"
    assert logview.parse("Nov  7 21:39:50 h kern.warn kernel: [    0.92] Core Release: 2.80a")["level"] == "INFO"          # (the drivers starting)
    assert logview.parse("Nov  7 23:39:50 h kern.warn kernel: [ 7200.5] throttled")["level"] == "WARNING"                # (the same severity, hours in)
    assert logview.parse("just some text")["text"] == "just some text"


def test_everything_or_problems_only_and_a_traceback_keeps_its_level(tmp_path: Path) -> None:
    f = _file(tmp_path)
    allof = logview.read_log("all", files=[f])
    assert allof["exists"] and allof["matched"] == 10 and allof["lines"][-1]["text"].endswith("card trouble")
    prob = logview.read_log("problems", files=[f])
    texts = [l["text"] for l in prob["lines"]]
    assert [l["level"] for l in prob["lines"]] == ["ERROR", "WARNING", "ERROR", "ERROR", "ERROR"]
    assert "aplay stopped; reopening the speaker (1 in a row)" in texts[0] and "ModuleNotFoundError: No module named 'sherpa_onnx'" in texts[3]
    assert "Process tts-worker:" not in texts and not any("GET /desktop" in t for t in texts)        # (plain lines and requests are info)


def test_search_limit_and_truncation(tmp_path: Path) -> None:
    f = _file(tmp_path)
    got = logview.read_log("all", "SPEAKER", files=[f])
    assert got["matched"] == 1 and got["lines"][0]["level"] == "ERROR"
    got = logview.read_log("all", "", 3, files=[f])
    assert got["shown"] == 3 and got["matched"] == 10 and got["truncated"] and got["lines"][-1]["text"].endswith("card trouble")
    assert logview.read_log("all", "x" * 500, files=[f])["matched"] == 0                              # (a long search is cut, not an error)
    assert logview.read_log("all", "", 10_000, files=[f])["shown"] == 10                              # (the limit is clamped)


def test_the_older_file_comes_first_and_a_missing_file_is_said(tmp_path: Path) -> None:
    old = tmp_path / "messages.0"
    old.write_text("Nov  6 10:00:00 h user.notice sleepradiopi: 2026-11-06 10:00:00,000 INFO a.b: older")
    new = _file(tmp_path)
    got = logview.read_log("all", files=[old, new])
    assert got["lines"][0]["text"] == "older" and got["matched"] == 11
    none = logview.read_log("all", files=[tmp_path / "nothing"])
    assert none == {"exists": False, "lines": [], "matched": 0, "shown": 0, "truncated": False}


def test_only_the_newest_part_of_a_huge_file_is_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(logview, "READ_MAX", 2000)
    f = tmp_path / "messages"
    f.write_text("".join(f"Nov  7 10:{i // 60:02d}:{i % 60:02d} h user.notice sleepradiopi: 2026-11-07 10:00:00,000 INFO a.b: line {i}\n" for i in range(500)))
    got = logview.read_log("all", files=[f], limit=1000)
    assert got["lines"][-1]["text"] == "line 499" and got["matched"] < 40


def test_the_log_from_the_web(tmp_path: Path, monkeypatch) -> None:
    f = _file(tmp_path)
    monkeypatch.setattr(logview, "LOG_FILES", (f,))
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def get(q):
            with urllib.request.urlopen(base + "/api/log" + q, timeout=5) as r:
                return json.loads(r.read())
        d = get("")
        assert d["exists"] and d["matched"] == 10 and "system_up_s" in d and d["program_up_s"] is None        # (not the appliance: nothing to say of the program)
        assert get("?level=problems")["matched"] == 5
        assert get("?q=avahi")["matched"] == 1
        assert get("?lines=2")["shown"] == 2 and get("?lines=abc")["shown"] == 10        # (not a number: the default)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_dropped_connection_is_one_short_line_not_a_traceback(caplog, capsys) -> None:
    import logging
    from http.server import BaseHTTPRequestHandler

    from sleepradiopi.web import server
    srv = server.RadioServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        try:
            raise ConnectionResetError(104, "Connection reset by peer")
        except ConnectionResetError:
            with caplog.at_level(logging.INFO, logger="sleepradiopi.web.server"):
                srv.handle_error(None, ("192.168.50.118", 35862))
        assert "192.168.50.118: the client dropped its connection (ConnectionResetError)" in caplog.text
        assert capsys.readouterr().err == ""                                      # (no traceback)
        try:
            raise ValueError("a real mistake")
        except ValueError:
            srv.handle_error(None, ("192.168.50.118", 35862))
        assert "ValueError: a real mistake" in capsys.readouterr().err            # (anything else is reported as before)
    finally:
        srv.server_close()
