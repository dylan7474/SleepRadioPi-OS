"""Play the station on the Pi's own sound card (the HiFiBerry MiniAmp).

The MiniAmp has no volume control of its own, so the volume is applied
here, to each ~23 ms slice just before it goes to aplay. Buffers are kept
small so a turn of the knob is heard within about a quarter of a second.

The speaker is a listener like a browser: while it plays, the show runs.
Pausing mutes it at once and leaves; if nobody else is listening the show
ends after the station's grace period, and the next press starts a new one.
"""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.audio.eq import Equalizer, clamp
from sleepradiopi.audio.testsignal import KINDS, SIDE_WORDS, TestSignal
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.settings import save_setting

log = logging.getLogger(__name__)

SLICE_FRAMES = 1024          # ~23 ms: how often the volume can change
PIPE_BYTES = 16384           # ~93 ms of audio queued in the pipe to aplay
ALSA_BUFFER_US = 250_000     # aplay's own buffer; smaller risks underruns on a Zero
DB_PER_STEP = 0.5            # volume 100 = full scale, 0 = silent
F_SETPIPE_SZ = 1031          # fcntl.F_SETPIPE_SZ (Linux), missing from older Pythons
SAVE_AFTER_S = 3.0           # save the volume once the knob has been still this long
SLEEP_FADE_S = 60.0          # the sleep timer fades the speaker out over its last minute
MAX_SLEEP_MIN = 600


def gain(volume: int) -> float:
    """Volume 0-100 to a linear gain, in even 0.5 dB steps (0 = silent)."""
    if volume <= 0:
        return 0.0
    return 10 ** ((min(volume, 100) - 100) * DB_PER_STEP / 20)


class SpeakerOutput:
    """An Output (start/write/stop) that plays through aplay."""

    def __init__(self, device: str = "default", volume: int = 30,
                 command: list[str] | None = None, mono: bool = False,
                 eq: Equalizer | None = None) -> None:
        self.command = command or [
            "aplay", "-q", "-D", device, "-t", "raw", "-f", "S16_LE",
            "-r", str(pcm.SAMPLE_RATE), "-c", str(pcm.CHANNELS),
            f"--buffer-time={ALSA_BUFFER_US}",
        ]
        self.volume = volume
        self.mono = mono                 # both speakers play (L + R) / 2
        self.eq = eq                     # bass/mid/treble, the speaker only
        self.fade_end: float | None = None  # sleep timer: silent at this time.monotonic()
        self.test: TestSignal | None = None  # a test sound, played instead of the show
        self.enabled = True
        self._running = False            # between the show's start() and stop()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _open(self) -> None:
        if self.eq is not None:
            self.eq.reset()              # don't replay the end of the last session
        self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE)
        try:
            fcntl.fcntl(self._proc.stdin, F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass
        log.info("speaker on")

    def _close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("speaker off")

    # --- Output protocol (the show thread) -----------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._running = True
            if self.enabled and self._proc is None:
                self._open()

    def write(self, block: np.ndarray) -> None:
        for i in range(0, len(block), SLICE_FRAMES):
            with self._lock:
                proc = self._proc
                g = gain(self.volume)
                if self.fade_end is not None:
                    g *= max(0.0, min(1.0, (self.fade_end - time.monotonic()) / SLEEP_FADE_S))
                test = self.test
            if proc is None:
                return
            x = block[i:i + SLICE_FRAMES].astype(np.float32)
            if test is not None:             # the box as it is: no mono mix, EQ or low cut
                x = test.next(len(x))
                if test.done:
                    with self._lock:
                        if self.test is test:
                            self.test = None
                    if self.eq is not None:
                        self.eq.reset()      # don't replay the show from before the test
                    log.info("speaker: %s finished", test.label)
            else:
                if self.mono:
                    x = np.repeat(x.mean(axis=1, keepdims=True), x.shape[1], axis=1)
                if self.eq is not None:
                    x = self.eq.process(x)
            part = np.clip(x * g, -32768, 32767).astype(np.int16)
            try:
                proc.stdin.write(part.tobytes())
            except (BrokenPipeError, ValueError, OSError):
                with self._lock:
                    if self._proc is proc:
                        log.error("aplay stopped; reopening the speaker")
                        self._close()
                        if self._running and self.enabled:
                            self._open()
                return

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._close()

    # --- pause ------------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            if not enabled:
                self._close()
            elif self._running and self._proc is None:
                self._open()


class TeeOutput:
    """Send the show to several outputs (the speaker and the MP3 stream)."""

    def __init__(self, *outputs) -> None:
        self.outputs = outputs

    def start(self) -> None:
        for o in self.outputs:
            o.start()

    def write(self, block: np.ndarray) -> None:
        for o in self.outputs:
            o.write(block)

    def stop(self) -> None:
        for o in self.outputs:
            o.stop()


class SpeakerControl:
    """Volume and pause for the speaker, from the knob or the web page.

    The volume is remembered in state_file (saved a few seconds after the
    last change, so turning the knob doesn't write to the card every click).
    Pause isn't remembered: the radio always plays at power-on. Mono/stereo
    and the EQ are settings, so they're saved in config_file (speaker_mono,
    speaker_eq).
    """

    def __init__(self, speaker: SpeakerOutput, join: Callable[[], None],
                 leave: Callable[[], None], state_file: Path | None = None,
                 default_volume: int = 30, config_file: Path | None = None) -> None:
        self.speaker = speaker
        self._join, self._leave = join, leave
        self.state_file = state_file
        self.config_file = config_file
        self.paused = True
        self.slept = False               # the last pause was the sleep timer's
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._sleep_timer: threading.Timer | None = None
        self._sleep_min = 0
        self._pause_after_clip = False   # a clip started while paused: pause again after it
        # Says a line in the DJ voice (int16 stereo), for the "sides" test's
        # "Left speaker" / "Right speaker"; None (or returning None) = no voice.
        self.speech: Callable[[str], np.ndarray | None] | None = None
        speaker.volume = self._load(default_volume)
        speaker.set_enabled(False)   # silent until play()

    def _load(self, default: int) -> int:
        try:
            return max(0, min(100, int(json.loads(self.state_file.read_text())["volume"])))
        except (AttributeError, OSError, ValueError, KeyError, TypeError):
            return default

    def _save(self) -> None:
        if self.state_file is not None:
            write_atomic(self.state_file, json.dumps({"volume": self.speaker.volume}))

    def save_now(self) -> None:
        """Save the volume straight away, e.g. before a shutdown."""
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        self._save()

    @property
    def volume(self) -> int:
        return self.speaker.volume

    def set_volume(self, volume: int) -> None:
        with self._lock:
            self.speaker.volume = max(0, min(100, int(volume)))
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(SAVE_AFTER_S, self._save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def set_mono(self, mono: bool) -> None:
        """Switch between mono and stereo at once, and save it in the config.
        Other keys in the file are kept as they are."""
        with self._lock:
            if self.speaker.mono == mono:
                return
            self.speaker.mono = mono
            self._save_setting("speaker_mono", mono)
        log.info("speaker: %s", "mono" if mono else "stereo")

    def set_eq(self, gains: dict) -> None:
        """Bass/mid/treble in dB (-12..12; missing bands keep their setting).
        Heard at once, and saved in the config."""
        eq = self.speaker.eq
        if eq is None:
            return
        with self._lock:
            new = clamp({**eq.gains, **gains})
            if new == eq.gains:
                return
            eq.set(new)
            self._save_setting("speaker_eq", new)
        log.info("speaker EQ: %s", new)

    def set_highpass(self, hz: float) -> None:
        """Low cut for the speaker in Hz (0 = off), e.g. ~140 Hz with the
        bass port. Heard at once, and saved in the config."""
        eq = self.speaker.eq
        if eq is None:
            return
        with self._lock:
            before = eq.highpass
            eq.set(eq.gains, hz)
            if eq.highpass == before:
                return
            self._save_setting("speaker_highpass_hz", eq.highpass)
        log.info("speaker low cut: %s", f"{eq.highpass} Hz" if eq.highpass else "off")

    def _save_setting(self, key: str, value) -> None:
        """Set one key in the config file, keeping the others as they are."""
        if self.config_file is not None:
            save_setting(self.config_file, key, value)

    def step(self, delta: int) -> None:
        self.set_volume(self.speaker.volume + delta)

    def play(self) -> None:
        self._pause_after_clip = False       # asked to play: stay playing after a clip
        self.slept = False
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            self.speaker.set_enabled(True)
        log.info("speaker: play")
        self._join()

    def pause(self) -> None:
        self.set_sleep(0)            # a pause (knob, page or the timer itself) ends the timer
        self.stop_test()
        with self._lock:
            if self.paused:
                return
            self.paused = True
            self.speaker.set_enabled(False)
        log.info("speaker: pause")
        self._leave()

    def start_test(self, kind: str) -> None:
        """Play a test sound (testsignal.KINDS) on the speaker instead of the
        show; starts the speaker if it was paused."""
        if kind not in KINDS:
            raise ValueError(f"unknown test sound {kind!r}")
        signal = TestSignal(kind, pcm.SAMPLE_RATE, pcm.CHANNELS,
                            speech=self._side_words() if kind == "sides" else None)
        self.play()
        with self._lock:
            self.speaker.test = signal
        log.info("speaker: test sound %s", kind)

    def _side_words(self) -> dict[str, np.ndarray]:
        """'Left speaker' / 'Right speaker' in the DJ voice (blocking, a few
        seconds on a Zero); empty if there's no voice, so the test is noise only."""
        if self.speech is None:
            return {}
        try:
            words = {side: self.speech(text) for side, text in SIDE_WORDS.items()}
        except Exception:
            log.exception("speaker: couldn't say the sides")
            return {}
        return {side: audio for side, audio in words.items() if audio is not None}

    def play_clip(self, clip) -> None:
        """Play ready-made audio (e.g. the spoken address) on the speaker instead
        of the show. If the radio was paused it plays anyway, then pauses again."""
        if self.paused:
            self.play()
            self._pause_after_clip = True
            threading.Thread(target=self._pause_when_clips_end, name="clip-pause", daemon=True).start()
        with self._lock:
            self.speaker.test = clip

    def _pause_when_clips_end(self) -> None:
        quiet_since = None
        deadline = time.monotonic() + 300
        while self._pause_after_clip and time.monotonic() < deadline:
            if self.speaker.test is None:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since > 0.5:     # not just between two clips
                    break
            else:
                quiet_since = None
            time.sleep(0.05)
        if self._pause_after_clip:
            self._pause_after_clip = False
            self.pause()

    def stop_test(self) -> None:
        with self._lock:
            stopped, self.speaker.test = self.speaker.test, None
        if stopped is not None:
            if self.speaker.eq is not None:
                self.speaker.eq.reset()
            log.info("speaker: test sound stopped")

    def set_sleep(self, minutes: float) -> None:
        """Sleep timer: fade the speaker out over the last minute, then pause.
        0 cancels it (back to full volume). Not remembered over a restart."""
        minutes = max(0.0, min(float(minutes), MAX_SLEEP_MIN))
        with self._lock:
            if self._sleep_timer is not None:
                self._sleep_timer.cancel()
                self._sleep_timer = None
            was = self._sleep_min
            self._sleep_min = minutes
            if minutes:
                self.speaker.fade_end = time.monotonic() + minutes * 60
                self._sleep_timer = threading.Timer(minutes * 60, self._sleep_done)
                self._sleep_timer.daemon = True
                self._sleep_timer.start()
            else:
                self.speaker.fade_end = None
        if minutes or was:
            log.info("speaker: sleep timer %s", f"{minutes:g} min" if minutes else "off")

    def _sleep_done(self) -> None:
        log.info("speaker: sleep timer ended")
        with self._lock:
            self._sleep_min = 0          # so the pause below doesn't log "off" as well
        self.slept = True                # (an audiobook steps back a minute: you'd dozed off)
        self.pause()

    def toggle(self) -> None:
        if self.paused:
            self.play()
        else:
            self.pause()

    def status(self) -> dict:
        status = {"volume": self.speaker.volume, "playing": not self.paused,
                  "mono": self.speaker.mono, "sleep_min": None, "sleep_left_s": None}
        test = self.speaker.test
        status["test"] = None if test is None else {
            "kind": test.kind, "label": test.label, "elapsed_s": round(test.elapsed_s, 1),
            "duration_s": test.duration_s, "hz": None if test.hz() is None else round(test.hz()),
            "note": test.note()}
        end = self.speaker.fade_end
        if end is not None and self._sleep_min:
            status["sleep_min"] = self._sleep_min
            status["sleep_left_s"] = max(0, round(end - time.monotonic()))
        if self.speaker.eq is not None:
            status["eq"] = dict(self.speaker.eq.gains)
            status["highpass_hz"] = self.speaker.eq.highpass
        return status
