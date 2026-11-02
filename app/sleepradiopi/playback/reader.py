"""Speaks an ebook ahead of the listener.

A producer thread turns the book's sentences (playback/ebooks.py's units) into speaker-ready audio with the
radio's own voice worker, a sentence at a time, and keeps a buffer of it in memory; the station takes blocks
from read() as the speaker wants them. The point is the buffer: a Pi Zero 2 W makes speech only a little
faster than it plays (1.0-1.3x with nothing else going on, ~0.9x of speech-time under the real load, the
pauses between sentences making up the difference), so the voice has to work AHEAD -- a measured 20-minute
run with a 45 s lead had no underruns. The producer stops when the buffer holds `target_lead_s`, so it works
in bursts and the CPU rests between them.

  * `start_lead_s` of speech is made before the first block is handed out (the first sound comes about
    that long, plus the voice's load, after play is pressed);
  * seek() throws the buffer away and starts again at the unit for the new position; a sentence that was
    being made for the old place is dropped (generation counter), never played;
  * position() is the estimated millisecond position of what was last handed out, moving smoothly through
    each sentence, so the radio's progress bar and places work in the same units as for any book;
  * if the voice fails the producer tries again, and after MAX_ERRORS in a row stops and says why (error).

Speech makes the buffer's memory: 16 kHz mono voice -> 44.1 kHz stereo int16 is 176 KB a second, 8 MB for 45 s.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.playback.ebooks import Ebook

log = logging.getLogger(__name__)

TARGET_LEAD_S = 45.0
START_LEAD_S = 8.0
MAX_ERRORS = 5
RETRY_S = 1.0


MAX_GAIN = 4.0                    # (the DJ's lines go up to 8: that lifts a quiet line's hiss with it)
SMOOTH = 0.95                     # how slowly the book's loudness estimate moves (per sentence)


class BookLevel:
    """Levels the voice for a whole book, not sentence by sentence. The DJ's levelling (pcm.speech_pcm) brings
    every line to the same loudness, right for a line of patter and wrong for a book: each short sentence is
    lifted to match the long ones, so the volume jumps about and a quiet sentence's background hiss is lifted
    with it. Here the gain follows a slow average of how loud the voice has been, so neighbours match."""

    def __init__(self) -> None:
        self.rms: float | None = None

    def __call__(self, samples: np.ndarray, rate: int, volume: float) -> np.ndarray:
        x = pcm.speech_filter(samples, rate)
        r = pcm.speech_rms(x)
        if r > 0.02:                                  # (a near-silent sentence tells nothing about the voice)
            self.rms = r if self.rms is None else SMOOTH * self.rms + (1 - SMOOTH) * r
        basis = self.rms or r
        gain = float(np.clip(pcm.SPEECH_TARGET_RMS / basis, 1.0, MAX_GAIN)) if basis > 1e-5 else 1.0
        return pcm.speech_level(x, volume, gain)


class Reader:
    def __init__(self, tts, voice: str, speech=None, volume: float = 0.5, speed: float = 1.0, pause_scale: float = 1.0,
                 target_lead_s: float = TARGET_LEAD_S, start_lead_s: float = START_LEAD_S) -> None:
        self.tts = tts                    # tts.worker.TtsWorker: synth(voice, text, speed) -> (float samples, rate)
        self.voice = voice
        self.speech = speech or BookLevel()   # (samples, rate, volume) -> int16 stereo at 44.1 kHz
        self.volume = volume
        self.speed = speed                # live: the next sentences made use the new one
        self.pause_scale = pause_scale    # (and this: x the pause after each sentence)
        self.target_lead_s, self.start_lead_s = target_lead_s, start_lead_s
        self.book: Ebook | None = None
        self.error = ""                   # why the voice gave up (empty while all is well)
        self.finished = False             # the last sentence has been handed out
        self._cv = threading.Condition()
        self._blocks: collections.deque = collections.deque()      # (unit index, i, n, int16 block)
        self._frames = 0                  # in the buffer
        self._gen = 0
        self._next = 0                    # the next unit to make
        self._made_all = False
        self._pos_ms = 0
        self._current: int | None = None   # the unit the last block was from
        self._started = False             # the first block has gone out (the start lead was met)
        self._stop = False
        self._thread: threading.Thread | None = None

    # --- running --------------------------------------------------------------------------

    def start(self, book: Ebook, pos_ms: int = 0) -> None:
        self.close()
        with self._cv:
            self.book = book
            self.error, self.finished, self._stop = "", False, False
            self._reset(book.unit_at(pos_ms))
            self._pos_ms = book.units[self._next].start_ms if book.units else 0
        self._thread = threading.Thread(target=self._produce, name="reader", daemon=True)
        self._thread.start()

    def _reset(self, unit: int) -> None:
        """(Under the lock.) Start again at a unit: the buffer goes, and so does anything being made."""
        self._gen += 1
        self._blocks.clear()
        self._frames = 0
        self._next = unit
        self._made_all = False
        self._started = False
        self.finished = False
        self._cv.notify_all()

    def seek(self, pos_ms: int) -> int:
        """Carry on from an estimated position (ms). Returns the position it starts at (a sentence's start)."""
        with self._cv:
            if self.book is None or not self.book.units:
                return 0
            i = self.book.unit_at(pos_ms)
            self._reset(i)
            self._pos_ms = self.book.units[i].start_ms
            return self._pos_ms

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        with self._cv:
            self._blocks.clear()
            self._frames = 0

    # --- what the station asks -------------------------------------------------------------

    def lead_s(self) -> float:
        with self._cv:
            return self._frames / pcm.SAMPLE_RATE

    def position(self) -> int:
        """Estimated ms position in the book of what was last handed out."""
        with self._cv:
            return self._pos_ms

    def unit_text(self) -> str:
        """The sentence being spoken now (for the page)."""
        with self._cv:
            i = self._current
        return self.book.units[i].text if self.book and i is not None and i < len(self.book.units) else ""

    def read(self, timeout: float = 0.05) -> np.ndarray | None:
        """The next block of audio, or None: still making the first seconds, or the buffer ran dry (the voice is
        behind: the station plays silence), or the book has ended (finished)."""
        with self._cv:
            if not self._started:
                ready = self._frames / pcm.SAMPLE_RATE >= self.start_lead_s or self._made_all
                if not ready:
                    self._cv.wait(timeout)
                    return None
                self._started = True
            if not self._blocks:
                if self._made_all and self.book is not None:
                    self.finished = True
                else:
                    self._cv.wait(timeout)
                return None
            ui, i, n, block = self._blocks.popleft()
            self._frames -= len(block)
            self._current = ui
            units = self.book.units
            span = (units[ui + 1].start_ms if ui + 1 < len(units) else self.book.total_ms) - units[ui].start_ms
            self._pos_ms = int(units[ui].start_ms + span * (i + 0.5) / n)
            self._cv.notify_all()                  # (the producer may have room now)
            return block

    # --- the producer ----------------------------------------------------------------------

    def _produce(self) -> None:
        errors = 0
        while True:
            with self._cv:
                while not self._stop and (self._made_all or self._frames / pcm.SAMPLE_RATE >= self.target_lead_s):
                    self._cv.wait(0.5)
                if self._stop:
                    return
                gen, i = self._gen, self._next
                book = self.book
                if i >= len(book.units):
                    self._made_all = True
                    self._cv.notify_all()
                    continue
                unit = book.units[i]
            try:
                samples, rate = self.tts.synth(self.voice, unit.text, self.speed)
                audio = self.speech(samples, rate, self.volume)
                audio = np.concatenate([audio, pcm.silence(unit.pause_s * self.pause_scale)])
                errors = 0
            except Exception as e:                                  # (the worker reloading, a bad sentence, ...)
                errors += 1
                log.warning("reader: couldn't speak %r: %s (%d)", unit.text[:40], e, errors)
                if errors >= MAX_ERRORS:
                    with self._cv:
                        self.error = f"the voice stopped working: {e}"
                        self._made_all = True
                        self._cv.notify_all()
                    return
                time.sleep(RETRY_S)
                if errors >= 2:                                      # a sentence that won't speak is skipped, not looped on
                    with self._cv:
                        if self._gen == gen:
                            self._next = i + 1
                continue
            pad = (-len(audio)) % pcm.CHUNK_FRAMES
            if pad:
                audio = np.concatenate([audio, np.zeros((pad, pcm.CHANNELS), np.int16)])
            n = len(audio) // pcm.CHUNK_FRAMES
            with self._cv:
                if self._gen != gen:                                 # seeked while this was being made: not wanted
                    continue
                for k in range(n):
                    self._blocks.append((i, k, n, audio[k * pcm.CHUNK_FRAMES:(k + 1) * pcm.CHUNK_FRAMES]))
                self._frames += len(audio)
                self._next = i + 1
                self._cv.notify_all()
