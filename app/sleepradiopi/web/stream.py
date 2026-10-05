"""The station's MP3 output: one ffmpeg encoder shared by every listener.

The station writes PCM here; write() paces it to real time (a little ahead,
to cover each track's decoder start-up), and a reader thread copies the
encoder's MP3 bytes to every connected listener's queue. A new listener
gets the last few seconds first, so playback starts at once.

Listening in a browser is off unless it's switched on (the "web_stream"
setting; always on without a speaker, or there'd be nothing to hear): the
radio is for its own speakers, and the encoder costs a Zero 2 W CPU all the
time. Switched off, this still paces the show to real time -- with the
speaker paused nothing else would -- but runs no encoder and takes no
listeners. It can be switched either way while the show runs.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections import deque

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

BITRATE = "128k"
LEAD_S = 2.0              # how far the station may run ahead of real time
CATCH_UP_S = 5.0          # if it falls further behind than this, stop trying to catch up
BURST_BYTES = 48_000      # ~3 s at 128 kbit/s, sent to a new listener straight away
CLIENT_QUEUE_CHUNKS = 400  # a listener this far behind is dropped


class Mp3Output:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._running = False                # between the show's start() and stop()
        self._proc: subprocess.Popen | None = None
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._recent: deque[bytes] = deque()
        self._recent_bytes = 0
        self._t0 = 0.0
        self._frames = 0

    # --- Output protocol (called by the station's show thread) --------------------------

    def start(self) -> None:
        self._running = True
        self._t0 = time.monotonic()
        self._frames = 0
        if self.enabled:
            self._start_encoder()

    def _start_encoder(self) -> None:
        self._proc = subprocess.Popen(
            [*pcm.FFMPEG, "-f", "s16le", "-ar", str(pcm.SAMPLE_RATE), "-ac", str(pcm.CHANNELS),
             "-i", "-", "-c:a", "libmp3lame", "-b:a", BITRATE, "-f", "mp3", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        threading.Thread(target=self._pump, args=(self._proc,), name="mp3-pump", daemon=True).start()
        log.info("encoder started")

    def write(self, block: np.ndarray) -> None:
        if not self._running:
            return
        ahead = self._t0 + self._frames / pcm.SAMPLE_RATE - time.monotonic()
        if ahead > LEAD_S:
            time.sleep(ahead - LEAD_S)
        elif ahead < -CATCH_UP_S:
            log.warning("stream fell %.1fs behind real time; resyncing", -ahead)
            self._t0 = time.monotonic() - self._frames / pcm.SAMPLE_RATE
        self._frames += len(block)
        proc = self._proc
        if proc is None:
            return
        try:
            proc.stdin.write(block.tobytes())
        except (BrokenPipeError, ValueError, OSError):
            if self._proc is proc:
                log.error("encoder went away")

    def stop(self) -> None:
        self._running = False
        self._stop_encoder()

    def set_enabled(self, enabled: bool) -> None:
        """Switch listening in a browser on or off, even mid-show. Off closes
        every listener's connection."""
        self.enabled = enabled
        if enabled and self._running and self._proc is None:
            self._start_encoder()
        elif not enabled:
            self._stop_encoder()
            with self._lock:
                clients, self._clients = list(self._clients), set()
            for q in clients:
                with q.mutex:
                    q.queue.clear()
                q.put_nowait(None)          # tells its connection to close

    def _stop_encoder(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._lock:
            self._recent.clear()
            self._recent_bytes = 0
        log.info("encoder stopped")

    # --- listeners ----------------------------------------------------------------------

    def _pump(self, proc: subprocess.Popen) -> None:
        while chunk := proc.stdout.read1(4096):
            with self._lock:
                self._recent.append(chunk)
                self._recent_bytes += len(chunk)
                while self._recent_bytes > BURST_BYTES:
                    self._recent_bytes -= len(self._recent.popleft())
                clients = list(self._clients)
            for q in clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    log.warning("dropping a listener that fell too far behind")
                    self.remove_client(q)
                    with q.mutex:
                        q.queue.clear()
                    q.put_nowait(None)  # tells its connection to close

    def drop_listeners(self) -> None:
        """Every browser tuned in is let go (its connection closes); the stream itself carries on."""
        with self._lock:
            clients, self._clients = list(self._clients), set()
        for q in clients:
            with q.mutex:
                q.queue.clear()
            q.put_nowait(None)

    def listeners(self) -> int:
        """How many browsers are tuned in."""
        with self._lock:
            return len(self._clients)

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(CLIENT_QUEUE_CHUNKS)
        with self._lock:
            for chunk in self._recent:
                q.put_nowait(chunk)
            self._clients.add(q)
        return q

    def remove_client(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)
