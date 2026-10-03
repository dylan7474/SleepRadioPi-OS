"""The buttons' spoken names, made ahead of time.

A button with steps says where it landed ("BBC Radio 2") instead of giving
pips -- but making speech takes seconds on a Zero, far too slow for a press.
So the names are made in the background when the buttons change, one at a
time once the show is under way, and kept as small files (raw int16 stereo)
that a press just plays. Until a name is ready -- or after the voice, its
speed or its volume changed, which makes them all again -- the press gets
None and gives its pips.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

WAIT_READY_S = 5.0        # how often to look whether the voice is free to make one
BETWEEN_S = 1.0           # a breath between names: the DJ's own lines come first


class Names:
    def __init__(self, render: Callable[[str], np.ndarray], folder: Path, key: Callable[[], str],
                 ready: Callable[[], bool] = lambda: True, between_s: float = BETWEEN_S) -> None:
        """render(text): the speech (blocking); key(): what the files depend on
        besides their words (the voice, its speed and volume); ready(): the
        show is playing, so making one won't hold up its opening."""
        self.render, self.folder, self.key, self.ready = render, folder, key, ready
        self.between_s = between_s
        self._wanted: set[str] = set()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def file(self, text: str) -> Path:
        return self.folder / (hashlib.sha1(f"{self.key()}|{text}".encode()).hexdigest()[:20] + ".raw")

    def has(self, text: str) -> bool:
        return self.file(text).is_file()

    def clip(self, text: str) -> np.ndarray | None:
        """The name, if it's been made; if not, None (and it's made for next time)."""
        try:
            data = np.fromfile(self.file(text), dtype="<i2")
        except OSError:
            self._make(text)
            return None
        return data[:len(data) // pcm.CHANNELS * pcm.CHANNELS].reshape(-1, pcm.CHANNELS)

    def want(self, texts) -> None:
        """All the names the buttons need now: the missing ones are made, and the
        files of names no longer on a button are deleted."""
        with self._lock:
            self._wanted = set(texts)
            keep = {self.file(t).name for t in self._wanted}
        try:
            for f in self.folder.glob("*.raw"):
                if f.name not in keep:
                    f.unlink(missing_ok=True)
        except OSError:
            log.exception("button names: couldn't tidy %s", self.folder)
        for text in sorted(self._wanted):
            if not self.has(text):
                self._make(text)

    def _make(self, text: str) -> None:
        with self._lock:
            self._wanted.add(text)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="button-names", daemon=True)
                self._thread.start()
        self._queue.put(text)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                while not self.ready():
                    time.sleep(WAIT_READY_S)
                path = self.file(text)
                if text not in self._wanted or path.is_file():      # (taken off its button, or made already)
                    continue
                audio = self.render(text)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(audio.astype("<i2").tobytes())
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                log.info("button name made: %s (%.1f s)", text, len(audio) / pcm.SAMPLE_RATE)
                time.sleep(self.between_s)
            except Exception:
                log.exception("button names: couldn't make %r", text)
            finally:
                self._queue.task_done()
