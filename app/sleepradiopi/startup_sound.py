"""The sound at power-on, so a slow start doesn't look like a broken radio.

The station takes the best part of a minute to go on air (most of it loading
the voice), so this runs first, as its own small program: a soft chime at
once, then -- if it has been made -- the DJ saying "Sleep Radio is warming
up...", in the DJ's own voice. That line is made by the station once the
show is running (never while it's starting) and kept in the cache, so from
the second start-up on it plays straight after the chime.

It plays at the speaker's saved volume (0 = silent), and not at all if the
speaker is off or "startup_sound" is false in the config. After the words it
ticks softly every few seconds (like a clock) while the station loads, until
the station asks it to stop (STOP) -- the station then ticks on itself until
the DJ's welcome is ready. While it plays it holds a lock file, so the
station waits for the sound card.

    python3 -m sleepradiopi.startup_sound [--home DIR] [--device NAME]

Only the standard library is imported unless there's a spoken line to play
(numpy then scales it), so the chime comes out as early as possible.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

RATE = 44_100
CHANNELS = 2
LOCK = Path(os.environ.get("SLEEPRADIOPI_STARTUP_LOCK", "/run/sleepradiopi/startup-sound.lock"))
STOP = LOCK.with_name("startup-sound.stop")     # (the station) "I'm taking the sound card now"
TICK_EVERY_S = 3.0
TICK_FOR_S = 90.0                                # at most (a station that never comes)
WARMING_UP = "Sleep Radio is warming up. The music will be with you in a moment."   # (the box's)


def warming_up(radio_name: str) -> str:
    return f"{radio_name} is warming up. The music will be with you in a moment."
DB_PER_STEP = 0.5             # the speaker's volume scale (audio/speaker.py)


def gain(volume: int) -> float:
    if volume <= 0:
        return 0.0
    return 10 ** ((min(volume, 100) - 100) * DB_PER_STEP / 20)


def chime(level: float) -> bytes:
    """Three soft bell notes rising (C, E, G), each ringing on: ~1.6 s."""
    notes = [(523.25, 0.00), (659.25, 0.18), (783.99, 0.36)]
    n = int(1.6 * RATE)
    out = array.array("h", bytes(2 * CHANNELS * n))
    peak = 0.28 * level * 32767
    for freq, start in notes:
        s0 = int(start * RATE)
        w = 2 * math.pi * freq / RATE
        for i in range(n - s0):
            t = i / RATE
            env = min(1.0, t / 0.008) * math.exp(-t * 3.2)          # quick attack, bell decay
            v = int(peak * env * (math.sin(w * i) + 0.25 * math.sin(2 * w * i)) / 1.25)
            j = 2 * (s0 + i)
            out[j] = max(-32768, min(32767, out[j] + v))
            out[j + 1] = out[j]
    return out.tobytes()


CHIME_FILE = "chime-v1.raw"


def tick(level: float) -> bytes:
    """A soft, short "tock" (as the station's warm-up tick), then silence to TICK_EVERY_S."""
    n = int(0.04 * RATE)
    out = array.array("h", bytes(2 * CHANNELS * int(TICK_EVERY_S * RATE)))
    for i in range(n):
        t = i / RATE
        v = int(0.12 * 32767 * level * math.exp(-t * 120)
                * (0.6 * math.sin(2 * math.pi * 1100 * t) + 0.4 * math.sin(2 * math.pi * 550 * t)))
        out[2 * i] = out[2 * i + 1] = v
    return out.tobytes()


def cached_chime(home: Path, level: float) -> bytes:
    """The chime at this level. Making it takes a Pi Zero 2.5 s in pure Python,
    so it's made once at full level and kept; scaling it takes ~0.4 s."""
    path = home / ".cache" / "sleepradiopi" / CHIME_FILE
    try:
        base = array.array("h", path.read_bytes())
    except OSError:
        base = array.array("h", chime(1.0))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(base.tobytes())
            os.replace(tmp, path)
        except OSError:
            pass
    if level >= 1.0:
        return base.tobytes()
    return array.array("h", [int(x * level) for x in base]).tobytes()


def speech_file(home: Path, voice: str | None, radio_name: str | None = None) -> Path:
    """The spoken line, made once per voice (and per name: a renamed radio says its new one)."""
    if radio_name is None:
        from sleepradiopi.config import brand
        radio_name = brand.name_from_config(home / ".config" / "sleepradiopi" / "config.json")
    tag = "" if radio_name == "Sleep Radio" else "-" + "".join(c for c in radio_name.lower() if c.isalnum())
    return home / ".cache" / "sleepradiopi" / f"startup-{voice or 'none'}{tag}.raw"


def settings(home: Path) -> tuple[bool, str | None, int]:
    """(play it?, voice, volume) from the config and the saved volume."""
    try:
        conf = json.loads((home / ".config" / "sleepradiopi" / "config.json").read_text())
    except (OSError, ValueError):
        conf = {}
    play = bool(conf.get("speaker_enabled", False)) and bool(conf.get("startup_sound", True))
    try:
        volume = int(json.loads((home / ".local" / "state" / "sleepradiopi" / "speaker.json").read_text())["volume"])
    except (OSError, ValueError, KeyError, TypeError):
        volume = int(conf.get("speaker_volume", 30))
    return play, conf.get("broadcast_voice", "stock"), max(0, min(100, volume))


def parts(home: Path):
    """The start-up sound as raw PCM, a piece at a time: the chime first, so it
    can play while the spoken line (which needs numpy) is got ready."""
    play, voice, volume = settings(home)
    level = gain(volume)
    if not play or level == 0:
        return
    yield cached_chime(home, level)
    speech = speech_file(home, voice)
    if speech.is_file():
        import numpy as np               # only now: it takes a moment to load on a Zero
        words = np.frombuffer(speech.read_bytes(), dtype=np.int16).astype(np.float32) * level
        yield bytes(2 * CHANNELS * int(0.2 * RATE)) + np.clip(words, -32768, 32767).astype(np.int16).tobytes()


def sound(home: Path) -> bytes | None:
    """The whole start-up sound, or None if there's to be none."""
    return b"".join(parts(home)) or None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Play the radio's start-up sound.")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--device", default="default")
    args = parser.parse_args(argv)
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        STOP.unlink(missing_ok=True)
        LOCK.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        player, pieces = None, 0
        for piece in parts(args.home):
            pieces += 1
            if player is None:
                player = subprocess.Popen(["aplay", "-q", "-D", args.device, "-t", "raw", "-f", "S16_LE",
                                           "-r", str(RATE), "-c", str(CHANNELS)], stdin=subprocess.PIPE)
            player.stdin.write(piece)
            player.stdin.flush()
        if player is not None:                   # then tick until the station takes over
            _, _, volume = settings(args.home)
            each = tick(gain(volume))
            step = 2 * CHANNELS * int(0.25 * RATE)
            end = time.monotonic() + TICK_FOR_S
            while not STOP.exists() and time.monotonic() < end:
                for i in range(0, len(each), step):          # (a quarter-second at a time: stops quickly)
                    if STOP.exists():
                        break
                    player.stdin.write(each[i:i + step])
                    player.stdin.flush()
                try:
                    LOCK.touch()                             # (still alive: not a crashed one's lock)
                except OSError:
                    pass
            player.stdin.close()
            player.wait(timeout=60)
            print(f"start-up sound played ({'chime + warming up' if pieces > 1 else 'chime'})", flush=True)
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


def wait_for_it(limit_s: float = 20.0) -> None:
    """(the station) Let a start-up sound finish before opening the sound card
    (its ticking stops when asked)."""
    try:
        if LOCK.exists():
            STOP.touch()
    except OSError:
        pass
    end = time.monotonic() + limit_s
    while LOCK.exists() and time.monotonic() < end:
        try:
            if time.time() - LOCK.stat().st_mtime > 60:    # left behind by a crash
                break
        except OSError:
            break
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
