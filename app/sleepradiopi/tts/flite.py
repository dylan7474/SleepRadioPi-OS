"""Flite (CMU's small speech synthesiser) as the voice an ebook is read in.

Why: the neural voices (Piper, run through sherpa-onnx in tts/worker.py) need 230-260 MB while they speak, which a
Pi Zero 2 W (467 MB, the station using 115+) can't give, and they speak each piece afresh, so the voice drifts from
sentence to sentence. Flite follows fixed rules: the same voice every time, 20-25 MB of memory, 7x faster than real
time on a Zero 2 W (measured 2026-11-03), and no model to load. It is robotic, as a small synthesiser is; the
neural voice stays the one for the DJ and the news.

Flite runs as a program (the image has /usr/bin/flite; Buildroot's package, with its voices compiled in), one
short-lived process per sentence: no memory is kept between sentences, and a bad one can't hurt the station.
It is given the text as an argument (never through a shell) and writes a WAV to a pipe.

The voices are called "flite-awb" (a Scottish man), "flite-rms" (an American man) and "flite-slt" (an American
woman). The pace is one of four names (slow, medium, normal, fast), turned into words a minute; the voices
speak at different paces at the same setting, so each is brought to the same words a minute.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import wave

import numpy as np

log = logging.getLogger(__name__)

PROGRAM = "flite"
ENV = "SLEEPRADIO_FLITE"             # a path to use instead (the tests)

# id -> (Flite's name for it, what it's called on the page, its natural words a minute at duration_stretch 1)
VOICES: dict[str, tuple[str, str, float]] = {
    "flite-awb": ("awb", "Flite: Scottish man", 221.0),
    "flite-rms": ("rms", "Flite: American man", 190.0),
    "flite-slt": ("slt", "Flite: American woman", 230.0),
}
DEFAULT_VOICE = "flite-awb"

# the pace names -> words a minute (an audiobook is read at 150-160, news at 160-180)
PACES: dict[str, int] = {"slow": 150, "medium": 175, "normal": 200, "fast": 230}
DEFAULT_PACE = "medium"

STRETCH_MIN, STRETCH_MAX = 0.6, 2.0
TIMEOUT_S = 60.0


def program() -> str | None:
    """Where Flite is, or None (a build without it: the ebook falls back to the stock voice)."""
    path = os.environ.get(ENV) or shutil.which(PROGRAM)
    return path if path and os.access(path, os.X_OK) else None


def available() -> bool:
    return program() is not None


def is_flite(voice: str) -> bool:
    return voice in VOICES


def label(voice: str) -> str:
    return VOICES[voice][1] if voice in VOICES else voice


def stretch(voice: str, pace: str) -> float:
    """Flite's duration_stretch (1 = its natural pace, higher = slower) that gives this voice this pace's words a minute."""
    natural = VOICES[voice][2] if voice in VOICES else 200.0
    wpm = PACES.get(pace, PACES[DEFAULT_PACE])
    return round(min(STRETCH_MAX, max(STRETCH_MIN, natural / wpm)), 3)


class FliteTts:
    """The same face as tts.worker.TtsWorker for the reader: synth(voice, text, speed) -> (float samples, rate).
    Here 'speed' is the duration_stretch to use (the station works it out from the voice and the pace)."""

    def __init__(self, exe: str | None = None) -> None:
        self.exe = exe or program()

    def synth(self, voice: str, text: str, speed: float) -> tuple[np.ndarray, int]:
        if self.exe is None:
            raise RuntimeError("flite isn't installed")
        if voice not in VOICES:
            raise ValueError(f"no Flite voice called {voice!r}")
        text = text.strip()
        if not text:
            return np.zeros(0, np.float32), 16000
        name = VOICES[voice][0]
        st = min(STRETCH_MAX, max(STRETCH_MIN, float(speed)))
        # (-t takes the text as one argument, so a sentence that starts with a dash is not an option; no shell)
        cmd = [self.exe, "-voice", name, "--setf", f"duration_stretch={st:.3f}", "-t", text, "-o", "/dev/stdout"]
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_S)
        if proc.returncode != 0 or len(proc.stdout) < 44:
            raise RuntimeError(f"flite failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-120:]}")
        with wave.open(io.BytesIO(proc.stdout), "rb") as w:
            rate, width, frames = w.getframerate(), w.getsampwidth(), w.readframes(w.getnframes())
            channels = w.getnchannels()
        if width != 2:
            raise RuntimeError(f"flite gave {width * 8}-bit audio")
        x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1)
        return x, rate
