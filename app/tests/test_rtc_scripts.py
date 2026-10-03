"""The image's real-time clock scripts (board/.../S12rtc, clock-save, rtc-stopped), run on the
PC with stand-ins for the chip and the system's tools."""
import os
import subprocess
import time
from pathlib import Path

import pytest

OVERLAY = Path(__file__).parent.parent.parent / "board" / "sleepradiopi" / "rootfs-overlay"
S12RTC, STOPPED, SAVE = OVERLAY / "etc/init.d/S12rtc", OVERLAY / "usr/sbin/rtc-stopped", OVERLAY / "usr/sbin/clock-save"


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
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "RTC": str(rtc), "STOPPED": str(STOPPED),
           "CLOCK": str(tmp_path / "clock"), "BUILT": str(tmp_path / "built"), "FLAG": str(tmp_path / "time-synced")}

    def run(script, *args):
        return subprocess.run(["sh", str(script), *args], env=env, capture_output=True, text=True).returncode
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


def test_ntp_sets_the_rtc_and_clears_the_flag(rig, monkeypatch) -> None:
    tmp, rtc, run, read = rig
    (tmp / "status").write_text("0x88")
    script = tmp / "clock-save"                              # (as installed, but with this rig's paths)
    script.write_text(SAVE.read_text().replace("/run/time-synced /run/ntp-synced", f"{tmp}/t1 {tmp}/t2")
                      .replace("[ -e /dev/rtc0 ]", "true").replace("F=/data/clock", f"F={tmp}/clock"))
    assert run(script, "periodic") == 0
    assert "-w -u" in read("hwclock.log") and read("status") == "0x08" and "had stopped" in read("log")
    assert abs(int(read("clock")) - time.time()) < 5
    assert run(script, "periodic") == 0 and read("log").count("had stopped") == 1     # (said once: it's clear now)
