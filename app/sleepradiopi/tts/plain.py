"""The plain voice: eSpeak, for the words that can't wait for the DJ.

The DJ's voice is a neural model: it has to be downloaded the first time the
radio is online, takes 20 s to load after power-on, and makes a sentence
slower than it says it on a Pi Zero 2 W. Setting the Wi-Fi up comes before
all of that -- on a new radio, before there is a DJ's voice at all -- so
those lines are said by eSpeak, which is in the image and makes a sentence in
a fraction of a second. It sounds like a robot, and that's fine: it's only
heard while setting up.
"""

from __future__ import annotations

import logging
import shutil
import struct
import subprocess

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

SPEED_WPM = 150
PEAK = 20000                 # of 32767: about as loud as the DJ


def command() -> str | None:
    return shutil.which("espeak") or shutil.which("espeak-ng")


def available() -> bool:
    return command() is not None


def from_wav(wav: bytes, rate: int = pcm.SAMPLE_RATE, channels: int = pcm.CHANNELS) -> np.ndarray | None:
    """eSpeak's WAV (mono 16-bit; its sizes aren't filled in when piped) as
    int16 at the speaker's rate and channels."""
    at = wav.find(b"data")
    if wav[:4] != b"RIFF" or at < 0 or len(wav) < at + 10:
        return None
    wav_rate = struct.unpack("<I", wav[24:28])[0]
    body = wav[at + 8:]
    x = np.frombuffer(body[:len(body) // 2 * 2], dtype="<i2").astype(np.float32)
    if not len(x) or not wav_rate:
        return None
    if wav_rate != rate:
        n = int(len(x) * rate / wav_rate)
        x = np.interp(np.arange(n) * (wav_rate / rate), np.arange(len(x)), x)
    peak = float(np.max(np.abs(x)))
    if peak > 0:
        x = x * (PEAK / peak)
    return np.repeat(x.astype(np.int16)[:, None], channels, axis=1)


def render(text: str, speed: int = SPEED_WPM, voice: str = "en") -> np.ndarray | None:
    """Speech as int16 at the speaker's rate, or None if eSpeak isn't there or fails."""
    exe = command()
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "-v", voice, "-s", str(speed), "--stdout", "--", text],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("espeak: %s", e)
        return None
    audio = from_wav(r.stdout)
    if audio is None:
        log.warning("espeak gave no speech (%s)", r.stderr.decode(errors="replace").strip()[:200])
    return audio
