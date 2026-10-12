"""The whole program, started for real: main.py is where everything is joined up, and nothing else runs it.

(Version 1.9.0 went out with a fault there -- a name imported inside main() that hid the module's own, so the
radio stopped as it started -- and every other test passed.)
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def test_the_radio_starts_and_answers(tmp_path: Path) -> None:
    with socket.socket() as s:                       # (a port nobody has)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "log"
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    with open(log, "w") as out:
        p = subprocess.Popen([sys.executable, "-m", "sleepradiopi.main", "--config", str(tmp_path / "config.json"), "--port", str(port)],
                             stdout=out, stderr=subprocess.STDOUT, env=env, cwd=tmp_path)
    try:
        status = None
        for _ in range(120):                         # (a minute at most: it answers in a second or two)
            if p.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                    status = json.loads(r.read())
                break
            except OSError:
                time.sleep(0.5)
        said = log.read_text()
        assert p.poll() is None, f"the program stopped as it started:\n{said[-3000:]}"
        assert status is not None and "version" in status, said[-3000:]
        for path in ("/desktop", "/api/rx/ft8", "/api/rx/report", "/api/rx/calibrate", "/api/radio"):      # the pages and what was joined up at the start
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                assert r.status == 200, path
        time.sleep(1.0)
        assert p.poll() is None and "UnboundLocalError" not in log.read_text() and "NameError" not in log.read_text()
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
