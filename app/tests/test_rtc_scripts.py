"""The image's real-time clock scripts (board/.../S12rtc, clock-save, rtc-stopped, rtc-sync), run on the
PC with stand-ins for the chip and the system's tools."""
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

OVERLAY = Path(__file__).parent.parent.parent / "board" / "sleepradiopi" / "rootfs-overlay"
S12RTC, STOPPED, SAVE = OVERLAY / "etc/init.d/S12rtc", OVERLAY / "usr/sbin/rtc-stopped", OVERLAY / "usr/sbin/clock-save"
SYNC = OVERLAY / "usr/sbin/rtc-sync"


@pytest.fixture
def rig(tmp_path: Path):
    """A pretend DS3231 on bus 1 at 0x68 (its status register in `status`), and a bin of stand-in tools."""
    dev = tmp_path / "devices" / "1-0068"
    (dev / "of_node").mkdir(parents=True)
    (dev / "of_node" / "compatible").write_bytes(b"maxim,ds3231\0")
    rtc = tmp_path / "rtc0"
    rtc.mkdir()
    (rtc / "device").symlink_to(dev)
    (tmp_path / "status").write_text("0x08")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    tools = {
        "i2cget": f'echo "$@" >> {tmp_path}/i2c.log; [ -e {tmp_path}/status ] || exit 1; cat {tmp_path}/status',
        "i2cset": f'echo "$@" >> {tmp_path}/i2c.log; printf "0x%02x" "$6" > {tmp_path}/status',
        "modprobe": "exit 0",
        "logger": f'shift 2; echo "$*" >> {tmp_path}/log',
        "hwclock": f'echo "$@" >> {tmp_path}/hwclock.log; exit 0',
        # setting the clock is only noted; everything else is the real date
        "date": f'case "$*" in *"-s "*) echo "$@" >> {tmp_path}/date.log;; *) exec /usr/bin/date "$@";; esac',
    }
    for name, body in tools.items():
        (bin_ / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_ / name).chmod(0o755)
    (tmp_path / "dev-rtc0").touch()
    for name in ("rtc-stopped", "rtc-sync", "clock-save"):   # (as installed: run by name, not through sh)
        (bin_ / name).write_text(f'#!/bin/sh\nexec sh {OVERLAY}/usr/sbin/{name} "$@"\n')
        (bin_ / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "RTC": str(rtc), "STOPPED": str(bin_ / "rtc-stopped"),
           "CLOCK": str(tmp_path / "clock"), "BUILT": str(tmp_path / "built"), "FLAG": str(tmp_path / "time-synced"),
           "RUN": str(tmp_path), "RTCDEV": str(tmp_path / "dev-rtc0"), "CLOCKLOG": str(tmp_path / "clock.log"),
           "SYNC": str(bin_ / "rtc-sync"), "SAVE": str(bin_ / "clock-save"), "RESYNC": str(bin_ / "rtc-sync"),
           "EDGE": "0", "RESYNC_EVERY": "0"}    # (no waiting for a second to tick over; no daily loop left running)

    def run(script, *args, **more):
        return subprocess.run(["sh", str(script), *args], env={**env, **more}, capture_output=True, text=True).returncode
    read = lambda name: (tmp_path / name).read_text() if (tmp_path / name).exists() else ""
    return tmp_path, rtc, run, read


def test_the_stop_flag_is_read_and_cleared(rig) -> None:
    tmp, rtc, run, read = rig
    assert run(STOPPED) == 1                                 # 0x08: running since it was set
    (tmp / "status").write_text("0x88")
    assert run(STOPPED) == 0 and read("status") == "0x88"    # it stopped (asking doesn't clear it)
    assert run(STOPPED, "clear") == 0 and read("status") == "0x08"       # (the other bits are kept)
    assert run(STOPPED, "clear") == 1 and run(STOPPED) == 1
    assert "-f -y 1 0x0068 0x0f" in read("i2c.log")          # bus and address from the device's name
    (tmp / "status").unlink()
    assert run(STOPPED) == 2                                 # the chip doesn't answer: can't tell
    (tmp / "status").write_text("0x88")
    (tmp / "devices" / "1-0068" / "of_node" / "compatible").write_bytes(b"nxp,pcf8523\0")
    assert run(STOPPED) == 2                                 # another kind of clock: not ours to judge


def test_boot_trusts_a_running_rtc_and_not_one_that_stopped(rig) -> None:
    tmp, rtc, run, read = rig
    now = int(time.time())
    (tmp / "built").write_text(str(now - 86400))
    (rtc / "since_epoch").write_text(str(now))
    assert run(S12RTC, "start") == 0
    assert (tmp / "time-synced").exists() and f"@{now}" in read("date.log") and "clock set from the RTC" in read("log")

    (tmp / "time-synced").unlink()
    (tmp / "date.log").unlink()
    (tmp / "status").write_text("0x88")                      # the battery went: a plausible time, but not to be believed
    assert run(S12RTC, "start") == 0
    assert not (tmp / "time-synced").exists() and read("date.log") == "" and "stopped" in read("log")

    (tmp / "status").unlink()                                # can't ask the chip: as before, by the time alone
    assert run(S12RTC, "start") == 0 and (tmp / "time-synced").exists()
    (tmp / "time-synced").unlink()
    (tmp / "status").write_text("0x08")
    (rtc / "since_epoch").write_text(str(now - 30 * 86400))  # running, but before this image was built: not sensible
    assert run(S12RTC, "start") == 0 and not (tmp / "time-synced").exists()


def test_ntp_sets_the_rtc_and_clears_the_flag(rig) -> None:
    tmp, rtc, run, read = rig
    (tmp / "status").write_text("0x88")
    assert run(SAVE, "periodic") == 0
    assert "-w -u" in read("hwclock.log") and read("status") == "0x08" and "had stopped" in read("log")
    assert abs(int(read("clock")) - time.time()) < 5 and abs(int(read("clock.ntp")) - time.time()) < 5
    assert (tmp / "time-synced").exists() and (tmp / "ntp-synced").exists()
    assert read("clock.log") == ""                           # (a clock that stopped: how far out it was means nothing)
    assert run(SAVE, "periodic") == 0 and read("log").count("had stopped") == 1     # (said once: it's clear now)


def test_how_far_out_the_rtc_was_is_noted_when_the_internet_time_comes_back(rig) -> None:
    tmp, rtc, run, read = rig
    now = int(time.time())
    (rtc / "since_epoch").write_text(str(now - 3))           # the RTC lost 3 s ...
    (tmp / "clock.ntp").write_text(str(now - 5 * 3600))      # ... in the 5 hours since NTP last set it
    assert run(SAVE, "step", offset="+2.500000") == 0        # (ntpd has just put the radio's clock forward 2.5 s)
    line = read("clock.log")
    assert re.search(r"the RTC was [34]\.00 s slow after 0 d 5 h 0 min without the internet time", line), line
    assert "the radio's own clock: 2.50 s slow" in line and "check the RTC" not in line
    assert line.split("  ", 1)[1] in read("log") and "-w -u" in read("hwclock.log")     # in the system log too; then it's set

    assert run(SAVE, "periodic", offset="+0.010000") == 0    # NTP is keeping it now: nothing more to note
    assert read("clock.log") == line

    (tmp / "ntp-synced").unlink()                            # the next power-on: not stepped, so the radio's clock
    (rtc / "since_epoch").write_text(str(now + 121))         # (and the RTC read against it) is still 1 s behind
    assert run(SAVE, "stratum", offset="+1.000000") == 0
    last = read("clock.log").splitlines()[-1]
    assert re.search(r"the RTC was (119|120)\.00 s fast after 0 d 0 h 0 min", last) and "check the RTC" in last

    (tmp / "ntp-synced").unlink()                            # no RTC fitted: nothing to note, the time is still saved
    (tmp / "dev-rtc0").unlink()
    assert run(SAVE, "step", offset="+9.000000") == 0 and len(read("clock.log").splitlines()) == 2


def test_with_no_internet_time_the_clock_is_re_read_from_the_rtc(rig) -> None:
    tmp, rtc, run, read = rig
    now = int(time.time())
    (rtc / "since_epoch").write_text(str(now + 2))
    assert run(SYNC) == 0 and read("date.log") == ""         # the time was never trusted: the RTC isn't either
    (tmp / "time-synced").touch()
    (tmp / "ntp-synced").touch()
    assert run(SYNC) == 0 and read("date.log") == ""         # ntpd set the clock minutes ago: its job
    os.utime(tmp / "ntp-synced", (now - 4 * 3600,) * 2)      # ... but not for 4 hours (Wi-Fi went)
    assert run(SYNC) == 0
    assert f"@{now + 2}" in read("date.log")
    assert re.search(r"clock re-read from the RTC: the radio's own clock had gone [12]\.00 s slow", read("clock.log"))
    assert abs(int(read("clock")) - now) < 5                 # and the time is saved for the next boot

    (tmp / "ntp-synced").unlink()                            # a radio that lives off Wi-Fi: the same
    (tmp / "date.log").unlink()
    (tmp / "status").write_text("0x88")
    assert run(SYNC) == 0 and read("date.log") == ""         # the RTC stopped: not believed
    (tmp / "status").write_text("0x08")
    (rtc / "since_epoch").write_text(str(now - 1200))
    assert run(SYNC) == 0 and read("date.log") == ""         # 20 minutes apart: which one is wrong? left alone
    assert "20 minutes apart" in read("clock.log").splitlines()[-1]
    assert run(SYNC, "secs", "-7") == 0 and run(SYNC, "bogus") == 1


def test_the_log_keeps_its_last_200_lines(rig) -> None:
    tmp, rtc, run, read = rig
    (tmp / "clock.log").write_text("".join(f"line {i}\n" for i in range(200)))
    assert run(SYNC, "note", "one more") == 0
    lines = read("clock.log").splitlines()
    assert len(lines) == 200 and lines[0] == "line 1" and lines[-1].endswith("one more")


def test_boot_starts_the_daily_re_read(rig) -> None:
    tmp, rtc, run, read = rig
    now = int(time.time())
    (tmp / "built").write_text(str(now - 86400))
    (rtc / "since_epoch").write_text(str(now))
    once = tmp / "bin" / "once"                              # (stands in for rtc-sync, and ends the loop)
    once.write_text(f"#!/bin/sh\necho ran >> {tmp}/resync.log\nkill $PPID\n")
    once.chmod(0o755)
    assert run(S12RTC, "start", RESYNC_EVERY="1", RESYNC=str(once)) == 0
    assert read("resync.log") == ""                          # not at boot: the clock has just been set
    time.sleep(2.5)
    assert read("resync.log") == "ran\n"
