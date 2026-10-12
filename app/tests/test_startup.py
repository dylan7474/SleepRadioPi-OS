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


_NO_NETWORK = r'''
"""Run inside a namespace with no network but its own loopback: the real program, its show, and everything
of it that would go online, asked for."""
import json, os, subprocess, sys, time, urllib.error, urllib.request, wave
import numpy as np
tmp, app, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
music = os.path.join(tmp, "music", "Test")
os.makedirs(music)
for n, pitch in enumerate((440, 660), 1):            # two short songs for the show to play
    with wave.open(os.path.join(music, f"0{n} - Tone {pitch}.wav"), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        x = (8000 * np.sin(2 * np.pi * pitch * np.arange(44100 * 4) / 44100)).astype("<i2")
        w.writeframes(np.repeat(x[:, None], 2, axis=1).tobytes())
conf = os.path.join(tmp, "config.json")
json.dump({"music_folder": os.path.join(tmp, "music"), "jingles_folder": os.path.join(tmp, "jingles"), "web_stream": True,
           "callsign": "M0ABC", "locator": "IO94", "report_heard": True, "conditions_every_h": 1,
           "calibrate_itself": {"http://192.168.50.4:8075": "listening"},
           "survey_itself": {"http://192.168.50.4:8075": {"every_h": 1, "places": [{"key": "ft8:20 m", "mode": "ft8", "band": "20 m", "hz": 14074000}]}},
           "radio_stations": [{"name": "A station", "url": "http://stream.example.org/radio"}]}, open(conf, "w"))
log = open(os.path.join(tmp, "log"), "w")
p = subprocess.Popen([sys.executable, "-m", "sleepradiopi.main", "--config", conf, "--port", str(port)], stdout=log, stderr=subprocess.STDOUT,
                     env={**os.environ, "HOME": os.path.join(tmp, "home"), "PYTHONPATH": app}, cwd=tmp)
base = f"http://127.0.0.1:{port}"
def call(path, body=None, timeout=20):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
out = {"started": False}
try:
    for _ in range(120):
        if p.poll() is not None:
            break
        try:
            call("/api/status", timeout=2); out["started"] = True; break
        except OSError:
            time.sleep(0.5)
    if out["started"]:
        import threading
        heard = [0]
        def listen():                                # (with no speaker here, the show plays for whoever listens in a browser)
            try:
                with urllib.request.urlopen(base + "/stream", timeout=10) as r:
                    until = time.time() + 14
                    while time.time() < until:
                        heard[0] += len(r.read(4096))
            except OSError:
                pass
        threading.Thread(target=listen, daemon=True).start()
        time.sleep(2)
        # everything that would go online, asked for with no network to go to
        out["conditions"] = call("/api/rx/conditions")[0]
        out["report"] = call("/api/rx/report")[1].get("on")
        out["survey"] = call("/api/rx/survey", {"base": "http://192.168.50.4:8075", "places": [{"key": "ft8:20 m", "mode": "ft8", "band": "20 m", "hz": 14074000}]})[0]
        out["calibrate"] = call("/api/rx/calibrate", {"base": "http://192.168.50.4:8075"})[0]
        out["update"] = call("/api/update/check", {})[0]
        out["directory"] = call("/api/radio/directory?q=jazz")[0] if True else None
        time.sleep(6)
        out["survey_after"] = {k: v for k, v in call("/api/rx/survey?base=http://192.168.50.4:8075")[1].items() if k in ("running", "error")}
        out["calibrate_after"] = {k: v for k, v in call("/api/rx/calibrate")[1].items() if k in ("running", "error")}
        out["sun"] = call("/api/rx/conditions")[1].get("sun")
        a = call("/api/status")[1]
        time.sleep(3)
        b = call("/api/status")[1]
        out["alive"] = p.poll() is None
        out["on_air"] = b.get("on_air")
        out["tracks"] = (b.get("library") or {}).get("tracks")
        out["now"] = (b.get("now") or {}).get("title")
        out["stream_bytes"] = heard[0]
        out["moving"] = ((a.get("now") or {}).get("title"), (a.get("now") or {}).get("elapsed_s")) != ((b.get("now") or {}).get("title"), (b.get("now") or {}).get("elapsed_s"))
finally:
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
log.close()
said = open(os.path.join(tmp, "log")).read()
out["tracebacks"] = said.count("Traceback (most recent call last)")
out["log_tail"] = said[-1500:]
print("RESULT " + json.dumps(out))
'''


def test_the_radio_plays_on_with_no_network_at_all(tmp_path: Path) -> None:
    """Some radios have no Wi-Fi and never will: the show from the card must go on, with everything that
    would go online (reports, NOAA's figures, a survey, a calibration, an update, the station directory)
    switched on or asked for and finding nothing there."""
    import shutil
    if not shutil.which("unshare") or not shutil.which("ip") or not shutil.which("ffmpeg"):
        pytest.skip("needs unshare, ip and ffmpeg")
    if subprocess.run(["unshare", "-r", "-n", "true"], capture_output=True).returncode:
        pytest.skip("this computer won't make a namespace without a network")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "home").mkdir()
    script = tmp_path / "no_network.py"
    script.write_text(_NO_NETWORK)
    done = subprocess.run(["unshare", "-r", "-n", sys.executable, str(script), str(tmp_path), str(Path(__file__).resolve().parent.parent), str(port)],
                          capture_output=True, text=True, timeout=180)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-2000:]
    out = json.loads(line[7:])
    assert out["started"] and out["alive"], out["log_tail"]
    shown = {k: v for k, v in out.items() if k != "log_tail"}
    assert out["tracks"] == 2 and out["on_air"] is True and out["now"] and out["moving"] and out["stream_bytes"] > 20_000, shown       # the show from the card, playing
    assert out["conditions"] == 200 and out["sun"] is None                   # NOAA not reached: no sun line, no harm
    assert out["report"] is True                                             # reporting on, with nowhere to report to
    assert out["survey"] == 200 and out["survey_after"]["running"] is False and "answering" in out["survey_after"].get("error", "")
    assert out["calibrate_after"]["running"] is False                        # the receiver isn't there: said, and over
    assert out["tracebacks"] <= 1, out["log_tail"]                           # (the PC's missing voice library is the one allowed)
