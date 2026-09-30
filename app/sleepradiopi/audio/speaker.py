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
from sleepradiopi.audio.noise import KINDS as NOISE_KINDS, NoiseGen
from sleepradiopi.audio.testsignal import KINDS, SIDE_WORDS, TestSignal
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.settings import save_setting

log = logging.getLogger(__name__)

KNOB_MODES = ("auto", "fine", "normal", "coarse")
# auto: the gap since the last click (same way) -> volume steps per click
KNOB_ACCEL = ((0.05, 5), (0.12, 3), (0.25, 2))

SLICE_FRAMES = 1024          # ~23 ms: how often the volume can change
PIPE_BYTES = 16384           # ~93 ms of audio queued in the pipe to aplay
ALSA_BUFFER_US = 500_000     # aplay's own buffer; 250 / 350 ms under-ran now and then while the voice reloaded
STUCK_S = 120.0              # the card won't open for this long: on_stuck() (the radio restarts)
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
    """An Output (start/write/stop) that plays through aplay.

    It also carries the noise layer (audio/noise.py): coloured noise mixed on
    top of whatever plays, set against it by a balance (0 = programme only,
    50 = both full, 100 = noise only). The sleep timer fades the programme,
    not the noise; and while the programme is paused -- the knob, the sleep
    timer, or no show at all -- a small pump thread keeps the noise going on
    its own. One writer at a time (the show or the pump), so the EQ sees one
    stream."""

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
        self.fade_end: float | None = None  # sleep timer: the programme silent at this time.monotonic()
        self.test: TestSignal | None = None  # a test sound, played instead of everything
        self.hold = None                 # the service menu's sound: plays (paused or not) instead of everything
        self.noise = None                # the noise layer (a NoiseGen), None = off
        self.noise_mix = 50              # balance: 0 = programme only, 50 = both full, 100 = noise only
        self.enabled = True
        self._running = False            # between the show's start() and stop()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._io = threading.Lock()      # one writer at a time: the show, or the noise pump
        self._pump: threading.Thread | None = None
        self._opened_once = False
        self.level = 0.0                 # the last slice's mean |sample|, before the volume (io/lamps.py)
        self.level_at = 0.0
        self._fails = 0                  # aplay failures in a row (the card won't open)
        self._opened_at = 0.0
        self._failing_since: float | None = None
        self.on_stuck = None             # called once when the card has failed to open for STUCK_S

    def _open(self) -> None:
        if not self._opened_once:        # the start-up sound (ticking by now) hands the card over
            from sleepradiopi import startup_sound
            startup_sound.wait_for_it()
            self._opened_once = True
        if self.eq is not None:
            self.eq.reset()              # don't replay the end of the last session
        # Unbuffered: Python 3.14 buffers pipes 128 KB (~0.75 s of audio), which would
        # delay every knob turn and pause by that much on top of the small pipe below.
        self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, bufsize=0)
        self._opened_at = time.monotonic()
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

    @property
    def _show_on(self) -> bool:
        return self._running and self.enabled

    def _sync(self) -> None:
        """(Under _lock.) aplay open while the show plays or the noise is on; the
        pump running while the noise is on and the show isn't."""
        want = self._show_on or self.noise is not None or self.hold is not None
        if want and self._proc is None:
            self._open()
        elif not want and self._proc is not None:
            self._close()
        if (self.noise is not None or self.hold is not None) and not self._show_on \
                and (self._pump is None or not self._pump.is_alive()):
            self._pump = threading.Thread(target=self._pump_run, name="noise", daemon=True)
            self._pump.start()

    def _pump_run(self) -> None:
        while True:
            with self._lock:
                if (self.noise is None and self.hold is None) or self._show_on or self._proc is None:
                    return
            if not self._emit(None, SLICE_FRAMES):   # blocks on aplay: real time
                time.sleep(0.1)

    # --- Output protocol (the show thread) -----------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._running = True
            self._sync()

    def write(self, block: np.ndarray) -> None:
        if not self.enabled:             # paused: the show's last seconds go nowhere
            return
        for i in range(0, len(block), SLICE_FRAMES):
            if not self._emit(block[i:i + SLICE_FRAMES]):
                return

    def _emit(self, show: np.ndarray | None, n: int = 0) -> bool:
        """Mix one slice (the programme, or None for noise only) and play it."""
        with self._io:
            with self._lock:
                proc = self._proc
                g = gain(self.volume)
                fade = 1.0
                if self.fade_end is not None:
                    fade = max(0.0, min(1.0, (self.fade_end - time.monotonic()) / SLEEP_FADE_S))
                test, noise, mix = self.test or self.hold, self.noise, self.noise_mix
                if show is not None and not self._show_on:
                    return False
            if proc is None:
                return False
            n = len(show) if show is not None else n
            if test is not None:             # the box as it is: no mono mix, EQ or low cut
                x = test.next(n)
                if test.done and test is not self.hold:
                    with self._lock:
                        if self.test is test:
                            self.test = None
                    if self.eq is not None:
                        self.eq.reset()      # don't replay the show from before the test
                    log.info("speaker: %s finished", test.label)
            else:
                prog = min(1.0, 2 * (1 - mix / 100)) if noise is not None else 1.0
                x = show.astype(np.float32) * (prog * fade) if show is not None \
                    else np.zeros((n, pcm.CHANNELS), np.float32)
                if noise is not None:
                    x = x + noise.next(n) * min(1.0, 2 * mix / 100)
                if self.mono:
                    x = np.repeat(x.mean(axis=1, keepdims=True), x.shape[1], axis=1)
                if self.eq is not None:
                    x = self.eq.process(x)
            self.level, self.level_at = float(np.mean(np.abs(x))), time.monotonic()   # (pre-volume: the VU needle)
            part = np.clip(x * g, -32768, 32767).astype(np.int16)
            try:
                view = memoryview(part.tobytes())
                while view:                          # (unbuffered: a write can take part of it)
                    view = view[proc.stdin.write(view):]
            except (BrokenPipeError, ValueError, OSError):
                self._failed()
                with self._lock:
                    if self._proc is proc:
                        self._close()
                        self._sync()
                return False
            if self._fails and time.monotonic() - self._opened_at > 5.0:   # (a dead aplay's pipe
                self._fails, self._failing_since = 0, None                  # takes a few writes)
            return True

    def _failed(self) -> None:
        """aplay stopped (e.g. the sound card wouldn't open): try again, waiting
        longer each time (not ten times a second, flooding the log), and if the
        card still won't open after STUCK_S, on_stuck() -- a stuck driver needs
        a restart. (Called with _io held: nothing else plays meanwhile anyway.)"""
        self._fails += 1
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        if self._fails & (self._fails - 1) == 0:      # the 1st, 2nd, 4th, 8th... time
            log.error("aplay stopped; reopening the speaker (%d in a row)", self._fails)
        if now - self._failing_since > STUCK_S and self.on_stuck is not None:
            stuck, self.on_stuck = self.on_stuck, None
            log.error("speaker: the sound card hasn't opened for %.0f s", now - self._failing_since)
            threading.Thread(target=stuck, name="speaker-stuck", daemon=True).start()
        time.sleep(min(5.0, 0.1 * 2 ** min(self._fails - 1, 6)))

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._sync()

    # --- pause and the noise ------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            self._sync()

    def set_noise(self, gen) -> None:
        """The noise layer on (a NoiseGen) or off (None)."""
        with self._lock:
            self.noise = gen
            self._sync()

    @property
    def fade_factor(self) -> float:
        """The sleep timer's fade: 1 until the last SLEEP_FADE_S, then down to 0."""
        if self.fade_end is None:
            return 1.0
        return max(0.0, min(1.0, (self.fade_end - time.monotonic()) / SLEEP_FADE_S))

    def set_hold(self, sound) -> None:
        """The service menu's sound (something with next(n)), played instead of
        everything -- even while paused -- until set back to None."""
        with self._lock:
            self.hold = sound
            self._sync()
        if sound is None and self.eq is not None:
            self.eq.reset()


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
                 default_volume: int = 30, config_file: Path | None = None,
                 noise: tuple[bool, str, int] = (False, "pink", 50),
                 knob_mode: str = "auto", knob_step: int = 2) -> None:
        self.speaker = speaker
        self.knob_mode = knob_mode if knob_mode in KNOB_MODES else "auto"
        self.knob_step = max(1, int(knob_step))
        self._knob_last = (0.0, 0)       # (when, direction) of the last click
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
        # The noise layer: (on, kind, balance) from the settings; it survives a restart.
        on, kind, mix = noise
        self.noise_kind = kind if kind in NOISE_KINDS else "pink"
        speaker.noise_mix = max(0, min(100, int(mix)))
        if on:
            speaker.set_noise(NoiseGen(self.noise_kind))

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

    def set_noise(self, on: bool | None = None, kind: str | None = None, mix: int | None = None) -> None:
        """The noise layer: on/off, its colour (audio/noise.py KINDS) and the
        balance against the programme (0-100). Heard at once, and saved."""
        if kind is not None and kind not in NOISE_KINDS:
            raise ValueError(f"no such noise: {kind}")
        with self._lock:
            if mix is not None:
                self.speaker.noise_mix = max(0, min(100, int(mix)))
                self._save_setting("noise_mix", self.speaker.noise_mix)
            changed_kind = kind is not None and kind != self.noise_kind
            if kind is not None:
                self.noise_kind = kind
                self._save_setting("noise_kind", kind)
            now_on = self.speaker.noise is not None
            want = now_on if on is None else bool(on)
            if on is not None:
                self._save_setting("noise_on", want)
        if want and (not now_on or changed_kind):
            self.speaker.set_noise(NoiseGen(self.noise_kind))
        elif not want and now_on:
            self.speaker.set_noise(None)
        if on is not None or changed_kind:
            log.info("speaker: noise %s", f"{self.noise_kind} on" if want else "off")

    def toggle_noise(self) -> bool:
        self.set_noise(on=self.speaker.noise is None)
        return self.speaker.noise is not None

    def step(self, delta: int) -> None:
        self.set_volume(self.speaker.volume + delta)

    def knob(self, clicks: int, now: float | None = None) -> None:
        """The knob turned: fine, normal or coarse steps, or (auto) by how fast it's
        turned -- a slow turn a step a click, a quick spin bigger ones."""
        if not clicks:
            return
        now = time.monotonic() if now is None else now
        direction = 1 if clicks > 0 else -1
        if self.knob_mode == "auto":
            last, last_dir = self._knob_last
            gap = now - last if last_dir == direction else 1.0
            per = next((n for limit, n in KNOB_ACCEL if gap < limit), 1)
        else:
            per = {"fine": 1, "normal": self.knob_step, "coarse": 5}[self.knob_mode]
        self._knob_last = (now, direction)
        self.step(clicks * per)

    def set_knob_mode(self, mode: str) -> None:
        if mode not in KNOB_MODES:
            raise ValueError(f"the knob is one of {', '.join(KNOB_MODES)}")
        self.knob_mode = mode
        self._save_setting("knob_mode", mode)
        log.info("speaker: knob %s", mode)

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
        status = {"volume": self.speaker.volume, "playing": not self.paused, "knob_mode": self.knob_mode,
                  "mono": self.speaker.mono, "sleep_min": None, "sleep_left_s": None,
                  "noise": {"on": self.speaker.noise is not None, "kind": self.noise_kind,
                            "mix": self.speaker.noise_mix,
                            "kinds": {k: {"label": v[0], "about": v[1]} for k, v in NOISE_KINDS.items()}}}
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
