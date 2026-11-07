"""Is something wrong? One small light in the desktop's menu bar, and /api/health behind it.

Nothing here fixes anything or speaks: it notices the things that go wrong quietly on a small radio and says them in
a sentence, so that nobody has to notice by accident -- the outside feeds that have stopped updating (no internet),
the card filling up, memory running out, the Pi too hot, the power supply dipping, the radio's program having
restarted itself, the clock not set. Healthy, the light isn't shown at all.

A thread (Health.start) takes a reading every SAMPLE_S, kept for WINDOW_S, so that a dip that came and went between two
looks is still seen; issues() is what the page asks for. Every reading comes from a function that gives None where the
machine has no such thing (a desktop), and the tests give their own.
"""
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SAMPLE_S = 10.0
WINDOW_S = 300.0                    # the lowest memory and the highest temperature are of the last five minutes
UNDERVOLT_SHOWN_S = 24 * 3600.0     # a dip in the power supply is shown for a day
RESTART_SHOWN_S = 3600.0            # the program restarting itself, for an hour
FEEDS_GRACE_S = 5 * 60.0            # after a start the feeds are not judged: what they kept in files may be an hour old, and the first fetch is under way
FEEDS_QUIET_S = 20 * 60.0           # feeds that never had anything are only a problem once the radio has run this long
DISK_WARN_MB, DISK_BAD_MB, DISK_WARN_PCT = 40.0, 15.0, 85.0      # (the data partition is small: 230 MB on the radio)
MEM_WARN_MB, MEM_BAD_MB = 30.0, 15.0
TEMP_WARN_C, TEMP_BAD_C = 75.0, 80.0           # (the Pi slows itself down at 80)
STALE_MOST = 3                                  # this many feeds stale at once: it's the internet, said once


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def mem_available_mb() -> float | None:
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return None


def oom_kills() -> int | None:
    for line in (_read("/proc/vmstat") or "").splitlines():
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return None


def temp_c() -> float | None:
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    return int(t) / 1000 if t and t.lstrip("-").isdigit() else None


def undervolt_now() -> bool | None:
    """The Pi's under-voltage alarm (the power supply sagging now), or None if this isn't a Pi."""
    try:
        for hw in sorted(Path("/sys/class/hwmon").iterdir()):
            if _read(str(hw / "name")) == "rpi_volt":
                v = _read(str(hw / "in0_lcrit_alarm"))
                return None if v is None else v.strip() == "1"
    except OSError:
        pass
    return None


def system_uptime() -> float | None:
    up = _read("/proc/uptime")
    return float(up.split()[0]) if up else None


def disk(path: Path) -> tuple[float, float] | None:
    """(MB free, % used) of the partition holding path (the radio's data), or None."""
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    return du.free / 1e6, 100.0 * du.used / du.total if du.total else 0.0


def ago(seconds: float) -> str:
    if seconds < 90:
        return "a minute" if seconds >= 30 else "a few seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


class Health:
    def __init__(self, data_dir: Path | None = None, clock: Callable[[], float] = time.time,
                 mem: Callable = mem_available_mb, temp: Callable = temp_c, volt: Callable = undervolt_now,
                 ooms: Callable = oom_kills, uptime: Callable = system_uptime, space: Callable = disk,
                 appliance: bool | None = None) -> None:
        self._dir = data_dir or Path.home()
        self.appliance = Path("/etc/sleepradiopi-version").exists() if appliance is None else appliance      # (a desktop has been up for days: it says nothing of the program restarting)
        self._clock, self._mem, self._temp, self._volt, self._ooms, self._uptime, self._space = clock, mem, temp, volt, ooms, uptime, space
        self._lock = threading.Lock()
        self._samples: list[tuple[float, float | None, float | None]] = []      # (when, MB available, °C)
        self.undervolt_at: float | None = None
        self.started = clock()
        self._sys_up_at_start = uptime()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        now = self._clock()
        try:
            mem, temp, volt = self._mem(), self._temp(), self._volt()
        except Exception:                                        # (a reading that can't be taken is no reason to stop)
            log.exception("health: a reading failed")
            return
        with self._lock:
            self._samples = [s for s in self._samples if now - s[0] <= WINDOW_S] + [(now, mem, temp)]
            if volt:
                self.undervolt_at = now

    def start(self) -> None:
        if self._thread is None:
            self.sample()
            self._thread = threading.Thread(target=self._run, name="health", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_S):
            self.sample()

    def issues(self, feeds: list = (), offline: bool = False, clock_ok: bool = True) -> dict:
        """-> {"level": "ok" | "warn" | "bad", "issues": [{"key", "level", "short", "says"}], "t"}; the worst first."""
        now = self._clock()
        found: list[dict] = []

        def add(key: str, level: str, short: str, says: str) -> None:
            found.append({"key": key, "level": level, "short": short, "says": says})

        with self._lock:
            samples = [s for s in self._samples if now - s[0] <= WINDOW_S]
            undervolt_at = self.undervolt_at
        # the card
        space = self._space(self._dir)
        if space is not None:
            free, used = space
            if free < DISK_BAD_MB:
                add("disk", "bad", f"Card nearly full: {free:.0f} MB left", f"The radio's card has only {free:.0f} MB free ({used:.0f}% used): saving settings, books' places and downloads will soon fail.")
            elif free < DISK_WARN_MB or used >= DISK_WARN_PCT:
                add("disk", "warn", f"Card filling up: {free:.0f} MB left", f"The radio's card is {used:.0f}% used, {free:.0f} MB free. Delete downloads or books you no longer want.")
        # memory
        low = min((s[1] for s in samples if s[1] is not None), default=None)
        if low is not None and low < MEM_WARN_MB:
            add("memory", "bad" if low < MEM_BAD_MB else "warn", f"Low on memory: {low:.0f} MB",
                f"The radio's free memory fell to {low:.0f} MB in the last five minutes; it may stutter, or the system may stop a program.")
        kills = self._ooms()
        if kills:
            add("oom", "warn", f"Out of memory ×{kills}", f"The system has stopped a program for lack of memory {kills} time{'s' if kills != 1 else ''} since the radio was switched on (the voice is the usual one; it starts again by itself).")
        # heat
        hot = max((s[2] for s in samples if s[2] is not None), default=None)
        if hot is not None and hot >= TEMP_WARN_C:
            add("heat", "bad" if hot >= TEMP_BAD_C else "warn", f"Hot: {hot:.0f}°C", f"The Pi reached {hot:.0f}°C in the last five minutes (it slows itself down at {TEMP_BAD_C:.0f}). Check the ventilation.")
        # power
        if undervolt_at is not None and now - undervolt_at <= UNDERVOLT_SHOWN_S:
            recent = now - undervolt_at < 60
            add("power", "bad" if recent else "warn", "Power supply dipping" if recent else f"Power dipped {ago(now - undervolt_at)} ago",
                ("The power supply is sagging right now (under-voltage)" if recent else f"The power supply dipped (under-voltage) {ago(now - undervolt_at)} ago")
                + ": a weaker supply or a longer or thinner cable than the Pi wants. It can cause hiss, dropouts and a corrupted card.")
        # the program restarting itself (not the whole radio: an update or a reboot starts the system's clock again too)
        up_now, up_then = self._uptime(), self._sys_up_at_start
        app_up = now - self.started
        if self.appliance and up_now is not None and up_then is not None and app_up < RESTART_SHOWN_S and up_then > 120:
            add("restart", "warn", f"Restarted {ago(app_up)} ago", f"The radio's program restarted itself {ago(app_up)} ago, without the radio being switched off (a crash, or the system stopping it for memory).")
        # the clock
        if not clock_ok:
            add("clock", "warn", "Clock not set", "The radio doesn't know the time yet (no internet or clock chip yet): the time, programmes and alarms can't be trusted.")
        # the outside feeds
        if not offline and now - self.started > FEEDS_GRACE_S:
            stale = []
            for f in feeds:
                if not f.wanted():
                    continue
                age = f.age()
                if age is None:
                    if now - self.started > FEEDS_QUIET_S:
                        stale.append((f.NAME, None))
                elif age > 2 * f.EVERY_S:
                    stale.append((f.NAME, age))
            if len(stale) >= STALE_MOST:
                oldest = max((a for _, a in stale if a is not None), default=None)
                add("feeds", "warn", "Can't reach the internet?", f"{len(stale)} outside feeds have stopped updating" + (f" (the oldest {ago(oldest)} out of date)" if oldest else "")
                    + ": " + ", ".join(n for n, _ in stale) + ". The radio's own things go on working; The world outside shows what it last had.")
            else:
                for name, age in stale:
                    add("feed:" + name, "warn", f"{name[0].upper() + name[1:]} out of date", f"{name[0].upper() + name[1:]} " + (f"were last had {ago(age)} ago." if age else "have never been had."))
        found.sort(key=lambda i: i["level"] != "bad")
        level = "bad" if any(i["level"] == "bad" for i in found) else "warn" if found else "ok"
        return {"level": level, "issues": found, "t": now}


health = Health()
