"""TTS in a separate, lower-priority process that is recycled when it grows.

Synthesis is the heaviest thing the station does, so it lives in its own
process: it can't hold up the realtime audio thread through the GIL, and
os.nice() gives the stream's decode/encode priority for the CPU.

Memory is the constraint on a Pi Zero 2 W (~464 MB usable). Measured there
(2026-09-22): a voice loads at ~120 MB, but the ONNX runtime's buffers grow
with every new phrase length and never shrink -- to ~300 MB for the
personal voice after one news bulletin. Two mitigations:

  * text is synthesised a clause at a time (split at , ; : . ! ?) with the
    pauses put back, which bounds the largest buffer -- the stock voice then
    stays ~226 MB even through a bulletin;
  * after each line the worker reports its RSS, and once it passes
    recycle_mb it is replaced by a fresh process (a reload takes ~15 s,
    hidden inside the current track because lines are made well ahead).

Measured again (2026-09-29): the personal voice still reached ~305 MB inside
one long line (a bulletin, the service menu's status report), which with the
station's ~100 MB left the Zero thrashing -- 48 s drop-outs and a page that
wouldn't load. So now clauses longer than MAX_WORDS are split at spaces too,
and the worker checks itself between clauses: past HARD_MB it stops, hands
back what it has made and the words still to say, and a fresh worker says
the rest. The peak stays near HARD_MB, at the cost of an extra reload in a
long line now and then.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

TTS_THREADS = 2  # leaves the other cores for ffmpeg decode/encode
RECYCLE_MB = 200
HARD_MB = 240        # mid-line: stop, and let a fresh worker say the rest
MAX_WORDS = 12       # a longer clause is split at spaces (bounds the largest buffer)
_CLAUSE = re.compile(r"(?<=[.!?,;:—])\s+")
PAUSE_S = {",": 0.12, ";": 0.18, ":": 0.18, "—": 0.18}
SENTENCE_PAUSE_S = 0.30


def clauses(text: str) -> list[tuple[str, float]]:
    """Split text into (clause, pause-after-seconds), keeping the punctuation."""
    parts = []
    for p in (p.strip() for p in _CLAUSE.split(text)):
        words = p.split()
        while len(words) > MAX_WORDS:
            parts.append(" ".join(words[:MAX_WORDS]))
            words = words[MAX_WORDS:]
        if words:
            parts.append(" ".join(words))
    out = []
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        out.append((p, 0.0 if last else PAUSE_S.get(p[-1], SENTENCE_PAUSE_S)))
    return out


def _rss_mb() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    return 0


def _serve(conn, voice_dir: str, voice: str) -> None:
    os.nice(5)
    from sleepradiopi.tts.engine import OfflineTtsEngine

    engine = OfflineTtsEngine(num_threads=TTS_THREADS)
    t0 = time.monotonic()
    ok = engine.ensure_loaded(Path(voice_dir), voice)
    conn.send(("ready" if ok else "missing", round(time.monotonic() - t0, 1), _rss_mb()))
    if not ok:
        return
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        text, speed = msg[0], msg[1]
        hard_mb = msg[2] if len(msg) > 2 else 0
        try:
            pieces, rate, rest = [], 22050, ""
            parts = clauses(text)
            for i, (clause, pause) in enumerate(parts):
                audio = engine.synth(clause, speed=speed)
                rate = audio.sample_rate
                pieces += [audio.samples, np.zeros(int(rate * pause), dtype=np.float32)]
                if hard_mb and i < len(parts) - 1 and _rss_mb() > hard_mb:
                    rest = " ".join(c for c, _ in parts[i + 1:])     # a fresh worker says these
                    break
            samples = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
            conn.send(("ok", samples.tobytes(), rate, _rss_mb(), rest))
        except Exception as e:
            conn.send(("error", f"{type(e).__name__}: {e}", 0, _rss_mb(), ""))


class TtsWorker:
    """One voice in a child process. synth() is blocking and thread-safe (calls are
    serialised); it waits while the worker is (re)loading."""

    def __init__(self, voices_dir: Path, voice: str, recycle_mb: int = RECYCLE_MB, hard_mb: int = HARD_MB) -> None:
        self.voices_dir = voices_dir
        self.voice = voice
        self.recycle_mb, self.hard_mb = recycle_mb, hard_mb
        self._ctx = mp.get_context("spawn")
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.restarts = 0
        self.last_rss_mb = 0
        self._conn = self._proc = None
        threading.Thread(target=self._start, name="tts-start", daemon=True).start()

    def _start(self) -> None:
        conn, child = self._ctx.Pipe()
        proc = self._ctx.Process(target=_serve, args=(child, str(self.voices_dir / self.voice), self.voice),
                                 name="tts-worker", daemon=True)
        proc.start()
        status, load_s, rss = conn.recv()
        if status != "ready":
            log.error("voice pack %r is missing or incomplete in %s", self.voice, self.voices_dir)
            return
        self._conn, self._proc, self.last_rss_mb = conn, proc, rss
        log.info("TTS worker ready: %s loaded in %.1fs (%d MB)", self.voice, load_s, rss)
        self._ready.set()

    def _recycle(self) -> None:
        """Replace the worker with a fresh one (in the background; synth() waits)."""
        with self._lock:
            self._shutdown_and_restart()

    def _shutdown_and_restart(self) -> None:
        old_conn, old_proc = self._conn, self._proc
        try:
            old_conn.send(None)
        except OSError:
            pass
        old_proc.join(timeout=5)
        if old_proc.is_alive():
            old_proc.kill()
        self.restarts += 1
        self._start()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def synth(self, voice: str, text: str, speed: float) -> tuple[np.ndarray, int]:
        if voice != self.voice:
            log.warning("asked for voice %r but only %r is loaded; using it", voice, self.voice)
        pieces, rate = [], 22050
        for _ in range(20):                      # (a very long text: a few fresh workers at most)
            if not self._ready.wait(timeout=120):
                raise RuntimeError("TTS worker not ready")
            with self._lock:
                self._conn.send((text, speed, self.hard_mb))
                status, payload, rate, rss, rest = self._conn.recv()
                self.last_rss_mb = rss
                recycle = rss > self.recycle_mb or bool(rest)
                if recycle:
                    self._ready.clear()
            if recycle:
                log.info("TTS worker at %d MB (> %d)%s: recycling", rss, self.recycle_mb,
                         " mid-line; a fresh one says the rest" if rest else "")
                threading.Thread(target=self._recycle, name="tts-recycle", daemon=True).start()
            if status != "ok":
                raise RuntimeError(payload)
            pieces.append(np.frombuffer(payload, dtype=np.float32))
            if not rest:
                break
            pieces.append(np.zeros(int(rate * SENTENCE_PAUSE_S), dtype=np.float32))
            text = rest
        return (np.concatenate(pieces) if len(pieces) > 1 else pieces[0]), rate

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.send(None)
            except OSError:
                pass
        if self._proc is not None:
            self._proc.join(timeout=5)
