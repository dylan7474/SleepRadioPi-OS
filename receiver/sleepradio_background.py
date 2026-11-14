#!/usr/bin/env python3
"""Sleep Radio background jobs: what a dongle does while nobody is listening to it.

Runs beside sleepradio_receiver.py on the computer that has the dongle (standard library only):

  * radiosondes: radiosonde_auto_rx (https://github.com/projecthorus/radiosonde_auto_rx) decodes the weather
    balloons' signals (400-406 MHz), during the hours around the twice-daily launches (--sonde-window), and for
    as long as a flight is still being heard;
  * a 433 MHz scan: rtl_433 listens for the little sensors (thermometers, weather stations, doorbells) the rest
    of the time. Every device is served with what it says (temperature, humidity, battery...), its signal and a made-up ID;
    ones named in known.json carry their name. (Tyre-pressure sensors are read but their real ID is never shown.)

Nothing is written to the SD card: what is heard is kept in memory and served live (whoever polls it keeps the
history), and the sonde decoder's own logs go to /run (RAM).

The receiver comes first. It asks (POST /yield) before it opens the dongle, the job lets go, and nothing is
started again until the receiver has rested (it lets go itself after a minute without a listener).

What it serves (port 8076, JSON, read-only except /yield, which only answers this computer):
  /status   which job has the dongle, whether the receiver is using it, the sonde windows
  /sondes   the flights heard (position, height, speed, and a thinned track)
  /sondes/series?id=ID[&since=T]   one flight's readings over time (only those after time T): a row every SERIES_EVERY_S seconds or so (time, position,
            height, speeds, heading, temperature, humidity, pressure, battery, signal), kept in memory while this runs
  /433      the devices heard
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("background")

PORT = 8076
TICK_S = 3.0                 # how often it looks at what the dongle should be doing
HOLD_OFF_S = 30.0            # after the receiver asked for the dongle: no job starts for this long (it is about to open it)
SONDE_QUIET_S = 300          # a flight heard this recently keeps the sonde job going past its window
RESTART_AFTER_S = 30.0       # a job that died isn't started again sooner than this
MAX_DEVICES = 500            # 433: devices remembered
TRACK_POINTS = 300           # sondes: points of a flight's track served
SERIES_EVERY_S = 2.5         # sondes: a row of a flight's readings at most this often (the decoder hears a frame a second; it is asked every TICK_S)
SERIES_MAX = 4000            # ...rows kept of one flight (past this every other one goes: about 3.3 h at 3 s before it thins)
FLIGHTS_KEPT_S = 6 * 3600    # sondes: a flight stays listed this long after it was last heard (the decoder forgets it when it restarts)
SERIES_FLIGHTS = 8           # ...flights whose readings are kept (the newest heard)
SERIES_COLUMNS = ("t", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "temp", "humidity", "pressure", "batt", "snr", "frame")
LOG_LIMIT = 256 * 1024       # 433: if a log file is asked for (--log-433), it is turned over at this size
SKIP_FIELDS = {"time", "model", "id", "channel", "mic", "rssi", "snr", "noise", "freq", "freq1", "freq2", "protocol", "subtype", "raw_msg", "codes", "data", "len",
               "count", "num_rows", "rows", "mod", "hop_index", "mcs", "sequence_num"}        # (rtl_433's own bookkeeping: what is left is what the device says)
MAX_FIELDS = 14


# --- when -------------------------------------------------------------------------------


def parse_window(text: str) -> tuple[int, int]:
    """'22:45-01:30' (UTC) -> (minute of the day it starts, minute it ends); it may run past midnight."""
    try:
        a, b = text.split("-")
        (h1, m1), (h2, m2) = (a.split(":"), b.split(":"))
        start, end = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
    except ValueError:
        raise ValueError(f"a window is HH:MM-HH:MM (UTC), not {text!r}") from None
    if not (0 <= start < 1440 and 0 <= end < 1440):
        raise ValueError(f"a window is within one day: {text!r}")
    return start, end


def in_windows(minute: int, windows: list[tuple[int, int]]) -> bool:
    for start, end in windows:
        if (start <= minute < end) if start <= end else (minute >= start or minute < end):
            return True
    return False


def choose(minute: int, windows: list[tuple[int, int]], receiver_busy: bool, sonde_heard: bool) -> str | None:
    """What the dongle should be doing: nothing (the receiver has it), the sonde job, or the 433 scan."""
    if receiver_busy:
        return None
    return "sondes" if sonde_heard or in_windows(minute, windows) else "scan433"


# --- 433: what is heard ---------------------------------------------------------------------


class Devices:
    """The 433 MHz devices heard, with everything each one says (its readings: temperature, humidity, battery...). `id` is made up
    (a hash with a salt made at every start, held in memory) to tell devices apart; `real` is what rtl_433 calls it ("Acurite-Tower:1234"),
    which is what a name in known.json ({"Acurite-Tower:1234": "Shed"}) is matched on. Tyre-pressure sensors (TPMS) are the exception:
    their IDs follow a car about, so they are counted and read but their real ID is never kept or shown."""

    def __init__(self, salt: bytes, known: dict[str, str] | None = None, log_path: Path | None = None) -> None:
        self.salt, self.known = salt, dict(known or {})
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self.total = 0
        self.since = time.time()
        self.log_path = log_path

    def feed(self, line: str, now: float | None = None) -> bool:
        """One line of rtl_433's JSON. True if it was a device."""
        try:
            msg = json.loads(line)
            model = str(msg["model"])
        except (ValueError, KeyError, TypeError):
            return False
        if not isinstance(msg, dict):
            return False
        now = time.time() if now is None else now
        real = f"{model}:{msg.get('id', msg.get('channel', ''))}"
        hide = "TPMS" in model.upper() or str(msg.get("type", "")).upper() == "TPMS"      # (a car's tyre sensors: a number that follows it about)
        key = hashlib.sha256(self.salt + real.encode()).hexdigest()[:8]
        name = self.known.get(real)
        rssi, snr = msg.get("rssi"), msg.get("snr")
        with self.lock:
            self.total += 1
            e = self.items.get(key)
            if e is None:
                if len(self.items) >= MAX_DEVICES:
                    del self.items[min(self.items, key=lambda k: self.items[k]["last"])]
                e = self.items[key] = {"id": key, "model": model, "count": 0, "first": now}
            e["count"] += 1
            e["last"] = now
            for field, value in (("rssi", rssi), ("snr", snr)):
                if isinstance(value, (int, float)):
                    e[field] = round(float(value), 1)
            if isinstance(msg.get("freq"), (int, float)):
                e["freq"] = round(float(msg["freq"]), 3)
            if not hide:
                e["real"] = real
            if name:
                e["name"] = name
            readings = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in msg.items()
                        if k not in SKIP_FIELDS and ((isinstance(v, (int, float)) and not isinstance(v, bool)) or (isinstance(v, str) and 0 < len(v) <= 24))}
            if readings:
                e["readings"] = dict(list(readings.items())[:MAX_FIELDS])
        self._log({"t": int(now), "id": key, "model": model, "rssi": rssi})
        return True

    def _log(self, row: dict) -> None:
        """A line per message in a small file that is turned over (the SD card gets little)."""
        if self.log_path is None:
            return
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT:
                self.log_path.replace(self.log_path.with_suffix(".old"))
            with self.log_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            self.log_path = None                      # (no log is better than a job that stops)

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            devices = sorted(({**e, "ago_s": round(now - e["last"])} for e in self.items.values()), key=lambda e: e["last"], reverse=True)
            return {"since": self.since, "messages": self.total, "devices": devices}


# --- sondes: what is heard ---------------------------------------------------------------------


def tidy(directory: Path, limit: int = 1_000_000) -> int:
    """The sonde decoder's logs are in RAM: a file past `limit` bytes is emptied (its scan log grows all day), and
    returns how many it emptied."""
    n = 0
    try:
        for f in directory.iterdir():
            if f.is_file() and f.stat().st_size > limit:
                f.write_text("")
                n += 1
    except OSError:
        pass
    return n


def _num(v, low: float = -998.0) -> float | None:
    """A reading from the decoder, or None: it uses -1 and -9999 for what a sonde doesn't report."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > low else None


def readings(t: dict) -> dict:
    """A decoder frame's readings, cleaned (what a sonde doesn't report is None)."""
    return {"vel_v": _num(t.get("vel_v")), "vel_h": _num(t.get("vel_h")), "heading": _num(t.get("heading")), "temp": _num(t.get("temp"), -200.0),
            "humidity": _num(t.get("humidity"), -0.5), "pressure": _num(t.get("pressure"), 0.0), "batt": _num(t.get("batt"), 0.0),
            "snr": t.get("snr") if isinstance(t.get("snr"), (int, float)) and not isinstance(t.get("snr"), bool) else None}


ROUND = {"lat": 5, "lon": 5, "alt": 1, "vel_v": 2, "vel_h": 2, "heading": 1, "temp": 1, "humidity": 1, "pressure": 2, "batt": 2, "snr": 1, "frame": 0}


def series_row(rec: dict) -> list | None:
    """One row of a flight's readings (SERIES_COLUMNS) from the decoder's archive entry, or None if it has no time or place."""
    try:
        t = rec["latest_telem"]
        when = float(rec["timestamp"])
        lat, lon, alt = float(t["lat"]), float(t["lon"]), float(t["alt"])
    except (KeyError, TypeError, ValueError):
        return None
    frame = t.get("frame")
    vals = {"lat": lat, "lon": lon, "alt": alt, **readings(t), "frame": frame if isinstance(frame, int) and not isinstance(frame, bool) and frame >= 0 else None}
    return [round(when, 1)] + [None if vals[k] is None else round(vals[k], ROUND[k]) for k in SERIES_COLUMNS[1:]]


class Series:
    """Each flight's readings over time, in memory: a row at most every SERIES_EVERY_S seconds, the newest SERIES_FLIGHTS flights."""

    def __init__(self) -> None:
        self.rows: dict[str, list[list]] = {}

    def add(self, archive: dict) -> None:
        for serial, rec in (archive or {}).items():
            if not isinstance(rec, dict):
                continue
            row = series_row(rec)
            if row is None:
                continue
            ident = str((rec.get("latest_telem") or {}).get("id", serial))[:20]
            rows = self.rows.setdefault(ident, [])
            if rows and row[0] - rows[-1][0] < SERIES_EVERY_S:
                continue
            rows.append(row)
            if len(rows) > SERIES_MAX:
                del rows[1:-1:2]                                    # (every other one, but the first and the last stay)
        if len(self.rows) > SERIES_FLIGHTS:
            for ident in sorted(self.rows, key=lambda k: self.rows[k][-1][0] if self.rows[k] else 0)[:-SERIES_FLIGHTS]:
                del self.rows[ident]

    def count(self, ident: str) -> int:
        return len(self.rows.get(ident, ()))

    def get(self, ident: str, since: float = 0.0) -> dict | None:
        """The flight's rows (only those after `since`, a time: what a live view hasn't had yet), or None if it has none."""
        rows = self.rows.get(ident)
        return None if rows is None else {"id": ident, "every_s": SERIES_EVERY_S, "columns": list(SERIES_COLUMNS),
                                          "rows": [list(r) for r in rows if r[0] > since]}


def flights_from(archive: dict, now: float | None = None) -> list[dict]:
    """radiosonde_auto_rx's /get_telemetry_archive -> the flights, the latest heard first."""
    now = time.time() if now is None else now
    out = []
    for serial, rec in (archive or {}).items():
        try:
            t = rec["latest_telem"]
            path = rec.get("path") or []
            step = max(1, len(path) // TRACK_POINTS)
            r = readings(t)
            vel_v = r["vel_v"]
            out.append({
                "id": str(t.get("id", serial)), "type": t.get("type"), "freq": t.get("freq"),
                "lat": t["lat"], "lon": t["lon"], "alt": t["alt"], **r, "snr": t.get("snr"),
                "state": "climbing" if vel_v is not None and vel_v > 1 else "falling" if vel_v is not None and vel_v < -1 else "steady",
                "last": rec.get("timestamp"), "ago_s": round(now - rec["timestamp"]) if rec.get("timestamp") else None,
                "track": path[::step][-TRACK_POINTS:],
            })
        except (KeyError, TypeError, AttributeError):
            continue
    return sorted(out, key=lambda f: f["last"] or 0, reverse=True)


# --- the jobs ---------------------------------------------------------------------------------


class Job:
    """A program that holds the dongle: started on its own session so that everything under it goes when it does."""

    def __init__(self, name: str, command: list[str], cwd: str | None = None, on_line=None, env: dict | None = None) -> None:
        self.name, self.command, self.cwd, self.on_line, self.env = name, command, cwd, on_line, env
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.died = 0.0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        log.info("starting %s: %s", self.name, " ".join(self.command))
        self.proc = subprocess.Popen(self.command, cwd=self.cwd, env={**os.environ, **(self.env or {})}, start_new_session=True, text=True,
                                     stdout=subprocess.PIPE if self.on_line else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.time()
        if self.on_line:
            threading.Thread(target=self._read, args=(self.proc,), name=self.name, daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.on_line(line)

    def stop(self, wait_s: float = 6.0) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        log.info("stopping %s", self.name)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except (ProcessLookupError, PermissionError):
            pass


class Background:
    def __init__(self, jobs: dict[str, Job], windows: list[tuple[int, int]], receiver_busy, sondes_archive,
                 devices: Devices, clock=time.time, log_dir: Path | None = None, decoder_task=lambda: None) -> None:
        self.decoder_task = decoder_task
        self.jobs, self.windows, self.receiver_busy, self.sondes_archive, self.devices = jobs, windows, receiver_busy, sondes_archive, devices
        self.clock, self.log_dir, self.ticks = clock, log_dir, 0
        self.lock = threading.RLock()
        self.current: str | None = None
        self.hold_until = 0.0
        self.flights: list[dict] = []
        self.series = Series()
        self.decoder: dict | None = None
        self.sonde_heard_at = 0.0
        self.busy = False

    def _minute(self) -> int:
        t = datetime.fromtimestamp(self.clock(), timezone.utc)
        return t.hour * 60 + t.minute

    def _stop_current(self) -> None:
        if self.current:
            self.jobs[self.current].stop()
        self.current = None

    def make_way(self) -> str | None:
        """The receiver wants the dongle: whatever has it lets go now, and nothing starts for a while."""
        with self.lock:
            was = self.current
            self._stop_current()
            self.hold_until = self.clock() + HOLD_OFF_S
            return was

    def tick(self) -> None:
        self.ticks += 1
        if self.log_dir is not None and self.ticks % 100 == 0:
            tidy(self.log_dir)
        busy = bool(self.receiver_busy())                             # (asked before the lock: a slow answer mustn't hold up /yield)
        archive = self.sondes_archive() if self.current == "sondes" else None
        task = self.decoder_task() if self.current == "sondes" else None
        with self.lock:
            now = self.clock()
            self.busy = busy
            self.decoder = decoder_from(task) if self.current == "sondes" else None
            if archive is not None and self.current == "sondes" and self.jobs["sondes"].alive:
                fresh = flights_from(archive, now)
                known = {f["id"] for f in fresh}
                old = [{**f, "ago_s": round(now - f["last"]) if f.get("last") else f.get("ago_s")} for f in self.flights   # (the decoder forgets a flight when it
                       if f["id"] not in known and f.get("last") and now - f["last"] < FLIGHTS_KEPT_S]                      # restarts: the service keeps it listed)
                self.flights = sorted(fresh + old, key=lambda f: f["last"] or 0, reverse=True)[:SERIES_FLIGHTS]
                self.series.add(archive)
                heard = [f["last"] for f in self.flights if f["last"]]
                if heard:
                    self.sonde_heard_at = max(self.sonde_heard_at, max(heard))
            sonde_heard = now - self.sonde_heard_at < SONDE_QUIET_S
            want = None if now < self.hold_until else choose(self._minute(), self.windows, self.busy, sonde_heard)
            if self.busy:
                self._stop_current()
                return
            if want != self.current:
                self._stop_current()
            if want and want in self.jobs:
                job = self.jobs[want]
                if not job.alive:
                    if job.proc is not None:                          # (it died by itself)
                        job.died, job.proc = now, None
                    if now - job.died >= RESTART_AFTER_S:
                        job.start()
                self.current = want

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:                                         # (a bad tick must not end the service)
                log.exception("tick")
            time.sleep(TICK_S)

    def status(self) -> dict:
        with self.lock:
            return {"job": self.current, "receiver_busy": self.busy, "held_s": max(0, round(self.hold_until - self.clock())),
                    "sonde_windows_utc": [f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in self.windows],
                    "sonde_heard_ago_s": round(self.clock() - self.sonde_heard_at) if self.sonde_heard_at else None, "decoder": self.decoder}

    def sondes(self) -> dict:
        with self.lock:
            return {"running": self.current == "sondes", "flights": [{**f, "series_rows": self.series.count(f["id"])} for f in self.flights]}

    def sonde_series(self, ident: str, since: float = 0.0) -> dict | None:
        with self.lock:
            return self.series.get(ident, since)

    def scan433(self) -> dict:
        with self.lock:
            return {"running": self.current == "scan433", **self.devices.snapshot(self.clock())}


# --- the web side --------------------------------------------------------------------------------


def make_handler(bg: Background):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "SleepRadioBackground/1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, data, code: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/sondes/series":
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                ident = (q.get("id") or [""])[0]
                try:
                    since = float((q.get("since") or ["0"])[0])
                except ValueError:
                    since = 0.0
                had = bg.sonde_series(ident, since)
                return self._json(had if had else {"error": "no readings of that flight"}, 200 if had else 404)
            route = {"/status": bg.status, "/sondes": bg.sondes, "/433": bg.scan433}.get(self.path.split("?")[0])
            self._json(route() if route else {"error": "/status, /sondes, /sondes/series?id= or /433"}, 200 if route else 404)

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/yield":
                return self._json({"error": "POST /yield"}, 404)
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "only this computer can ask for the dongle"}, 403)
            self._json({"ok": True, "was": bg.make_way()})

    return Handler


def receiver_busy_check(url: str):
    """Is the receiver using its dongle (running, or a listener)? Not answering is not busy."""
    def busy() -> bool:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=2) as r:
                st = json.load(r)
            return bool(st.get("running") or st.get("listeners"))
        except (OSError, ValueError):
            return False
    return busy


def decoder_from(tasks) -> dict | None:
    """radiosonde_auto_rx's /get_task_list -> what its dongle is doing: {"task": "Decoding (405.099 MHz)", "freq_mhz", "type"} or None."""
    if not isinstance(tasks, dict) or not tasks:
        return None
    t = next(iter(tasks.values()))
    if not isinstance(t, dict) or not isinstance(t.get("task"), str):
        return None
    freq = t.get("freq")
    return {"task": t["task"][:60], "freq_mhz": round(freq / 1e6, 3) if isinstance(freq, (int, float)) and freq > 0 else None,
            "type": t.get("type") if isinstance(t.get("type"), str) else None}


def task_fetcher(url: str):
    def fetch():
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/get_task_list", timeout=3) as r:
                return json.load(r)
        except (OSError, ValueError):
            return None
    return fetch


def archive_fetcher(url: str):
    def fetch() -> dict:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/get_telemetry_archive", timeout=3) as r:
                return json.load(r)
        except (OSError, ValueError):
            return {}
    return fetch


def load_known(path: Path) -> dict[str, str]:
    try:
        known = json.loads(path.read_text())
        return {str(k): str(v) for k, v in known.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sleep Radio background jobs: sondes and a 433 MHz scan on a dongle nobody is listening to.")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--receiver", default="http://127.0.0.1:8074", help="the receiver that shares this dongle")
    ap.add_argument("--device", default="0", help="the dongle (its serial, or its place in the list)")
    ap.add_argument("--ppm", type=int, default=0)
    ap.add_argument("--gain", type=float, default=29.0)
    ap.add_argument("--sonde-window", action="append", default=[], metavar="HH:MM-HH:MM",
                    help="UTC hours the sonde job has the dongle (more than once); launches are about 11:15 and 23:15")
    ap.add_argument("--auto-rx-dir", type=Path, default=Path("/opt/radiosonde/radiosonde_auto_rx/auto_rx"))
    ap.add_argument("--auto-rx-python", default="/opt/radiosonde/venv/bin/python")
    ap.add_argument("--auto-rx-url", default="http://127.0.0.1:5000")
    ap.add_argument("--rtl-433", default="rtl_433")
    ap.add_argument("--freq-433", default="433.92M")
    ap.add_argument("--auto-rx-log", type=Path, default=Path("/run/sleepradio-background/autorx"),
                    help="where the sonde decoder keeps its logs: in RAM, so the SD card is not written")
    ap.add_argument("--known", type=Path, default=Path("/etc/sleepradio-background/known.json"),
                    help="the 433 devices that are yours: {\"Acurite-Tower:1234\": \"Shed\"} (read, never written)")
    ap.add_argument("--log-433", type=Path, help="keep a line per 433 message in this file (off: nothing is written)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    windows = [parse_window(w) for w in args.sonde_window]
    args.auto_rx_log.mkdir(parents=True, exist_ok=True)
    devices = Devices(secrets.token_bytes(16), load_known(args.known), args.log_433)
    jobs = {
        "sondes": Job("sondes", [args.auto_rx_python, "auto_rx.py", "-c", "station.cfg", "-l", str(args.auto_rx_log)], cwd=str(args.auto_rx_dir),
                      env={"PYTHONDONTWRITEBYTECODE": "1"}),
        "scan433": Job("scan433", [args.rtl_433, "-d", f":{args.device}" if not args.device.isdigit() else args.device,
                                   "-f", args.freq_433, "-g", str(args.gain), "-p", str(args.ppm), "-C", "si", "-M", "level", "-F", "json"],
                       on_line=devices.feed),
    }
    bg = Background(jobs, windows, receiver_busy_check(args.receiver), archive_fetcher(args.auto_rx_url), devices, log_dir=args.auto_rx_log,
                    decoder_task=task_fetcher(args.auto_rx_url))
    httpd = ThreadingHTTPServer(("", args.port), make_handler(bg))
    httpd.daemon_threads = True
    threading.Thread(target=bg.run, name="tick", daemon=True).start()
    log.info("Sleep Radio background jobs on port %d (sonde windows %s)", args.port, bg.status()["sonde_windows_utc"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bg.make_way()
    return 0


if __name__ == "__main__":
    sys.exit(main())
