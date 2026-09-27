import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi import startup_sound as ss


def _home(tmp_path: Path, conf: dict | None = None, volume: int | None = 80) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "sleepradiopi").mkdir(parents=True)
    (home / ".config" / "sleepradiopi" / "config.json").write_text(
        json.dumps({"speaker_enabled": True, "broadcast_voice": "personal", **(conf or {})}))
    if volume is not None:
        (home / ".local" / "state" / "sleepradiopi").mkdir(parents=True)
        (home / ".local" / "state" / "sleepradiopi" / "speaker.json").write_text(json.dumps({"volume": volume}))
    return home


def test_chime_is_soft_stereo_and_follows_the_volume() -> None:
    loud = np.frombuffer(ss.chime(1.0), dtype=np.int16).reshape(-1, 2)
    quiet = np.frombuffer(ss.chime(ss.gain(60)), dtype=np.int16).reshape(-1, 2)
    assert abs(len(loud) / ss.RATE - 1.6) < 0.01
    assert (loud[:, 0] == loud[:, 1]).all()
    assert 0.2 * 32767 < np.abs(loud).max() < 0.5 * 32767      # never near clipping
    assert abs(np.abs(quiet).max() / np.abs(loud).max() - ss.gain(60)) < 0.02
    assert abs(loud[0, 0]) < 200 and abs(loud[-1, 0]) < 1500      # fades in and out


def test_nothing_when_off_silent_or_no_speaker(tmp_path: Path) -> None:
    assert ss.sound(_home(tmp_path / "a", {"startup_sound": False})) is None
    assert ss.sound(_home(tmp_path / "b", volume=0)) is None
    assert ss.sound(_home(tmp_path / "c", {"speaker_enabled": False})) is None
    assert ss.sound(_home(tmp_path / "d")) is not None                  # on by default


def test_the_spoken_line_follows_the_chime(tmp_path: Path) -> None:
    home = _home(tmp_path, volume=100)
    words = np.full((ss.RATE, 2), 1000, dtype=np.int16)                  # 1 s of "speech"
    path = ss.speech_file(home, "personal")
    path.parent.mkdir(parents=True)
    path.write_bytes(words.tobytes())
    pcm = np.frombuffer(ss.sound(home), dtype=np.int16).reshape(-1, 2)
    assert abs(len(pcm) / ss.RATE - (1.6 + 0.2 + 1.0)) < 0.01
    assert (pcm[-ss.RATE:] == 1000).all()
    assert ss.speech_file(home, "stock") != path                         # one per voice


def test_main_plays_through_aplay_and_holds_the_lock(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    out = tmp_path / "played.raw"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aplay").write_text(f"#!/bin/sh\ntest -e {tmp_path / 'lock'} && cat > {out}\n")
    (bin_dir / "aplay").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(ss, "LOCK", tmp_path / "lock")
    assert ss.main(["--home", str(home)]) == 0
    assert out.stat().st_size == len(ss.chime(ss.gain(80)))              # played while locked
    assert (home / ".cache" / "sleepradiopi" / ss.CHIME_FILE).is_file()  # made once, kept
    assert not (tmp_path / "lock").exists()


def test_the_station_waits_for_it(tmp_path: Path, monkeypatch) -> None:
    lock = tmp_path / "lock"
    monkeypatch.setattr(ss, "LOCK", lock)
    lock.write_text("1")
    threading.Timer(0.3, lock.unlink).start()
    t0 = time.monotonic()
    ss.wait_for_it(5)
    assert 0.25 < time.monotonic() - t0 < 2
    lock.write_text("1")
    old = time.time() - 120
    import os
    os.utime(lock, (old, old))                                            # left behind by a crash
    t0 = time.monotonic()
    ss.wait_for_it(5)
    assert time.monotonic() - t0 < 0.5


def test_the_kept_chime_scales_like_a_fresh_one(tmp_path: Path) -> None:
    home = _home(tmp_path)
    first = ss.cached_chime(home, ss.gain(70))                            # makes and keeps it
    again = ss.cached_chime(home, ss.gain(70))                            # from the file
    fresh = np.frombuffer(ss.chime(ss.gain(70)), dtype=np.int16).astype(int)
    kept = np.frombuffer(again, dtype=np.int16).astype(int)
    assert first == again and np.abs(kept - fresh).max() <= 3   # rounding, twice: inaudible
