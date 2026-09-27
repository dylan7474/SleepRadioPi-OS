"""PCM plumbing for the broadcast: decode, level, trim, and voice processing.

Everything runs through ffmpeg subprocesses (already on Raspberry Pi OS),
so no audio codec work happens in Python. The station's common format is
16-bit stereo at 44.1 kHz.

Loudness levelling and edge-silence trimming are ports of SleepRadio's
TrackProbe.kt (Phases 12 and 14); the voice EQ is VoiceEq.kt's curve,
expressed as ffmpeg filters.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from sleepradiopi.config.atomic import write_atomic

log = logging.getLogger(__name__)

SAMPLE_RATE = 44_100
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS
CHUNK_FRAMES = 4096  # ~93 ms

# --- TrackProbe.kt constants ---------------------------------------------------------
SCAN_RATE = 8_000
WINDOW_MS = 20
SILENCE_FLOOR = 0.00316  # ~ -50 dBFS
LEAD_MIN_MS, LEAD_MAX_MS, LEAD_PAD_MS = 300, 5_000, 50
TRIM_MIN_MS, TRIM_MAX_MS, TAIL_PAD_MS = 400, 25_000, 150
BODY_SCAN_MS = 120_000
LOUDNESS_TARGET_RMS = 0.11  # ~ -19 dBFS
MIN_ITEM_GAIN, MAX_ITEM_GAIN = 0.2, 4.0
GAIN_LIMIT_KNEE = 0.85

# --- DjVoicePlayer.kt / VoiceEq.kt -----------------------------------------------------
SPEECH_TARGET_RMS = 0.30  # ~ -10 dBFS, before the announcer volume
SPEECH_MAX_GAIN = 8.0
VOICE_EQ = ("highpass=f=120,"
            "equalizer=f=250:t=q:w=1:g=-5,"
            "equalizer=f=3500:t=q:w=1:g=4,"
            "highshelf=f=8000:g=2")

FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]


@dataclass(frozen=True)
class TrackScan:
    gain: float
    start_ms: int  # 0 = don't clip the start
    end_ms: int    # 0 = play to the natural end
    duration_ms: int

    @property
    def playable_ms(self) -> int:
        end = self.end_ms or self.duration_ms
        return max(0, end - self.start_ms)


NO_SCAN = TrackScan(1.0, 0, 0, 0)


def sound_bounds(window_mean_sq: np.ndarray, window_ms: int = WINDOW_MS) -> tuple[int, int]:
    """First-sound start and last-sound end in ms; (-1, -1) when all silent."""
    loud = np.nonzero(window_mean_sq > SILENCE_FLOOR ** 2)[0]
    if loud.size == 0:
        return -1, -1
    return int(loud[0]) * window_ms, (int(loud[-1]) + 1) * window_ms


def edge_trim(file_end_ms: int, first_sound_ms: int, last_sound_end_ms: int) -> tuple[int, int]:
    """Clip points; 0 = leave that edge alone. Only trims silence in the ranges
    the app found worth trimming, and leaves a decay pad."""
    start = max(0, first_sound_ms - LEAD_PAD_MS) if LEAD_MIN_MS <= first_sound_ms <= LEAD_MAX_MS else 0
    end = 0
    if 0 < last_sound_end_ms < file_end_ms:
        if TRIM_MIN_MS <= file_end_ms - last_sound_end_ms <= TRIM_MAX_MS:
            end = min(last_sound_end_ms + TAIL_PAD_MS, file_end_ms)
    if end and start >= end:
        return 0, 0
    return start, end


def rms_to_gain(rms: float) -> float:
    if rms < 1e-4:
        return 1.0
    return float(np.clip(LOUDNESS_TARGET_RMS / rms, MIN_ITEM_GAIN, MAX_ITEM_GAIN))


def soft_limit(x: np.ndarray) -> np.ndarray:
    """Float samples in full-scale units -> int16, tanh-compressing anything past the
    knee instead of clipping (GainAudioProcessor.limitSample)."""
    knee = GAIN_LIMIT_KNEE * 32767.0
    span = 32767.0 - knee
    mag = np.abs(x)
    over = mag > knee
    if over.any():
        x = x.copy()
        x[over] = np.sign(x[over]) * (knee + span * np.tanh((mag[over] - knee) / span))
    return np.clip(np.rint(x), -32768, 32767).astype(np.int16)


def apply_gain(block: np.ndarray, gain: float) -> np.ndarray:
    if abs(gain - 1.0) < 1e-3:
        return block
    return soft_limit(block.astype(np.float32) * gain)


def _nice() -> None:
    os.nice(10)


def analyse(path: Path) -> TrackScan:
    """Decode a low-rate mono copy and measure loudness + silent edges. Streams the
    decode a window at a time, so memory stays small however long the track is."""
    win = SCAN_RATE * WINDOW_MS // 1000
    read_bytes = win * 2 * 500  # 10 s of 8 kHz mono per read
    proc = subprocess.Popen(
        [*FFMPEG, "-i", str(path), "-ac", "1", "-ar", str(SCAN_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, preexec_fn=_nice,
    )
    windows: list[np.ndarray] = []
    samples = 0
    pending = b""
    with proc:
        while data := proc.stdout.read(read_bytes):
            pending += data
            usable = len(pending) - len(pending) % (win * 2)
            if usable:
                x = np.frombuffer(pending[:usable], dtype=np.int16).astype(np.float32) / 32768.0
                windows.append((x.reshape(-1, win) ** 2).mean(axis=1))
                samples += len(x)
                pending = pending[usable:]
    samples += len(pending) // 2
    if proc.returncode != 0 or samples == 0:
        log.warning("scan failed for %s (ffmpeg exit %s)", path.name, proc.returncode)
        return NO_SCAN
    duration_ms = int(samples * 1000 / SCAN_RATE)
    if not windows:
        return TrackScan(1.0, 0, 0, duration_ms)
    mean_sq = np.concatenate(windows)
    body = mean_sq[: BODY_SCAN_MS // WINDOW_MS]
    loud = body[body > SILENCE_FLOOR ** 2]
    gain = rms_to_gain(float(np.sqrt(loud.mean())) if loud.size else 0.0)
    first, last = sound_bounds(mean_sq)
    start, end = edge_trim(duration_ms, first, last) if first >= 0 else (0, 0)
    return TrackScan(round(gain, 3), start, end, duration_ms)


class ScanCache:
    """Scans persisted to JSON, keyed by path + size + mtime, so each file is only
    decoded once (a full-library scan on a Zero is slow)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self._data: dict = json.loads(path.read_text())
        except (OSError, ValueError):
            self._data = {}

    @staticmethod
    def _key(p: Path) -> str:
        st = p.stat()
        return f"{p}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, p: Path) -> TrackScan | None:
        with self._lock:
            d = self._data.get(self._key(p))
        return TrackScan(**d) if d else None

    def scan(self, p: Path) -> TrackScan:
        if (hit := self.get(p)) is not None:
            return hit
        result = analyse(p)
        with self._lock:
            self._data[self._key(p)] = asdict(result)
            write_atomic(self.path, json.dumps(self._data))
        return result


def decode(path: Path, start_ms: int = 0, end_ms: int = 0) -> Iterator[np.ndarray]:
    """Stream a file as int16 blocks of shape (frames, 2) at 44.1 kHz, clipped to
    [start_ms, end_ms] (0 = no clip). Closing the generator stops ffmpeg."""
    cmd = [*FFMPEG]
    if start_ms:
        cmd += ["-ss", f"{start_ms / 1000:.3f}"]
    if end_ms:
        cmd += ["-to", f"{end_ms / 1000:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        chunk = CHUNK_FRAMES * BYTES_PER_FRAME
        pending = b""
        while data := proc.stdout.read(chunk):
            pending += data
            usable = len(pending) - len(pending) % BYTES_PER_FRAME
            if usable:
                yield np.frombuffer(pending[:usable], dtype=np.int16).reshape(-1, CHANNELS)
                pending = pending[usable:]
    finally:
        proc.kill()
        proc.wait()


def speech_pcm(samples: np.ndarray, sample_rate: int, volume: float) -> np.ndarray:
    """TTS output (mono float) -> broadcast-voice EQ, levelled like DjVoicePlayer,
    scaled by the announcer volume, as int16 stereo at 44.1 kHz."""
    proc = subprocess.run(
        [*FFMPEG, "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "-",
         "-af", VOICE_EQ, "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        input=np.asarray(samples, dtype=np.float32).tobytes(), capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"voice EQ failed: {proc.stderr.decode(errors='replace')[-200:]}")
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    rms = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
    gain = float(np.clip(SPEECH_TARGET_RMS / rms, 1.0, SPEECH_MAX_GAIN)) if rms > 1e-5 else 1.0
    return soft_limit(x * (gain * volume * 32767.0)).reshape(-1, CHANNELS)


def silence(seconds: float) -> np.ndarray:
    return np.zeros((int(SAMPLE_RATE * seconds), CHANNELS), dtype=np.int16)


def blocks(pcm: np.ndarray) -> Iterator[np.ndarray]:
    for i in range(0, len(pcm), CHUNK_FRAMES):
        yield pcm[i:i + CHUNK_FRAMES]
