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
from sleepradiopi.audio.eq import DEFAULT_BY_TYPE, DEFAULT_SLOTS, PRESETS, Equalizer, clamp, expand, preset_bands, validate_by_type, validate_slots
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
SHOW_STALL_S = 0.25          # the show hasn't written for this long (a book seeking, a station
                             # connecting): the pump plays a clip on, so a button's words aren't held up
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
                 eq: Equalizer | None = None, amp=None) -> None:
        self.amp = amp                   # an ampmute.AmpMute: the amp's MUTE pin, low while the card is closed
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
        self._show_at = 0.0              # when the show last came to write (time.monotonic())
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
        if self.amp is not None:
            self.amp.unmute_soon()       # (a moment after the card opens: its pop goes unheard)
        log.info("speaker on")

    def _close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if self.amp is not None:
            self.amp.mute()              # (before the card closes: the amp hisses while it idles)
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
        pump running while the noise is on and the show isn't, or while a clip
        plays (it carries the clip on if the show stalls)."""
        clip = self.hold is not None or self.test is not None
        want = self._show_on or self.noise is not None or clip
        if want and self._proc is None:
            self._open()
        elif not want and self._proc is not None:
            self._close()
        if (clip or (self.noise is not None and not self._show_on)) \
                and (self._pump is None or not self._pump.is_alive()):
            self._pump = threading.Thread(target=self._pump_run, name="noise", daemon=True)
            self._pump.start()

    def _pump_run(self) -> None:
        while True:
            with self._lock:
                clip = self.hold is not None or self.test is not None
                if (self.noise is None and not clip) or (self._show_on and not clip) or self._proc is None:
                    if not self._show_on:
                        self._sync()         # (a clip over while paused: the speaker off again)
                    return
                stalled = not self._show_on or time.monotonic() - self._show_at > SHOW_STALL_S
            if not stalled:                  # the show is writing: it plays the clip
                time.sleep(0.05)
                continue
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
            self._show_at = time.monotonic()     # (before waiting for _io: the pump makes way)
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
                if show is None and test is None and self._show_on:
                    return True              # (the pump, its clip just over: the show plays on)
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

    def play_test(self, sound) -> None:
        """A test sound or clip, instead of the show -- or, paused, on its own:
        the show stays paused (no burst of it before or after)."""
        with self._lock:
            self.test = sound
            self._sync()

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
        self.eq_slots = list(DEFAULT_SLOTS)   # the four buttons in EQ mode (main sets it from the settings)
        # The sound by what's playing (main sets these from the settings): a sound picked by hand holds
        # until the kind of audio changes; then that kind's sound (if it has one) takes over.
        self.eq_auto = True
        self.eq_by_type = dict(DEFAULT_BY_TYPE)
        self.audio_type: str | None = None
        self._explicit = False            # ... because a button asked for it (set_button_sound)
        self._auto_on = False             # the sound now playing came from eq_by_type or a button
        self._baseline: dict | None = None   # the listener's own sound, to go back to
        self._mine = threading.local()    # (this thread is applying a type's sound, not the listener changing it)
        self._join, self._leave = join, leave
        self.state_file = state_file
        self.config_file = config_file
        self._paused = True              # the speaker's own state (see paused)
        self.elsewhere = None            # web/hear.HearHere: a browser has the radio instead of the speaker
        self.slept = False               # the last pause was the sleep timer's
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._sleep_timer: threading.Timer | None = None
        self._sleep_min = 0
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

    def set_mono(self, mono: bool, save: bool = True) -> None:
        """Switch between mono and stereo at once, and save it in the config.
        Other keys in the file are kept as they are."""
        self._by_hand()
        with self._lock:
            if self.speaker.mono == mono:
                return
            self.speaker.mono = mono
            if save:
                self._save_setting("speaker_mono", mono)
        log.info("speaker: %s", "mono" if mono else "stereo")

    def set_eq(self, gains: dict, save: bool = True) -> None:
        """Band gains in dB (-12..12; missing bands keep their setting).
        Heard at once, and saved in the config (save=False: not yet: EQ mode on the
        buttons saves once, when it's left, so trying sounds doesn't write the card)."""
        eq = self.speaker.eq
        if eq is None:
            return
        self._by_hand()
        with self._lock:
            new = clamp({**eq.gains, **expand(gains)})
            if new == eq.gains:
                return
            eq.set(new)
            if save:
                self._save_setting("speaker_eq", new)
        log.info("speaker EQ: %s", new)

    def set_highpass(self, hz: float, save: bool = True) -> None:
        """Low cut for the speaker in Hz (0 = off), e.g. ~140 Hz with the
        bass port. Heard at once, and saved in the config."""
        eq = self.speaker.eq
        if eq is None:
            return
        self._by_hand()
        with self._lock:
            before = eq.highpass
            eq.set(eq.gains, hz)
            if eq.highpass == before:
                return
            if save:
                self._save_setting("speaker_highpass_hz", eq.highpass)
        log.info("speaker low cut: %s", f"{eq.highpass} Hz" if eq.highpass else "off")

    def set_evenness(self, value: str, save: bool = True) -> None:
        """Even out the volume of streams (audio/compressor.py): "off", "gentle" or "strong". Heard at once (ValueError if
        it isn't one of them), and saved in the config."""
        from sleepradiopi.audio import compressor
        value = compressor.mode(value)
        if compressor.MODE == value:
            return
        compressor.MODE = value
        if save:
            self._save_setting("stream_evenness", value)
        log.info("streams evened out: %s", value)

    # --- the buttons' EQ mode (io/presets.py): the sound on each button -----------------------

    def set_eq_slots(self, slots) -> list:
        """Which sound each of the four buttons picks in EQ mode. Saved."""
        self.eq_slots = validate_slots(slots)
        self._save_setting("eq_slots", self.eq_slots)
        log.info("EQ mode buttons: %s", self.eq_slots)
        return self.eq_slots

    def find_sound(self, name: str | None) -> dict | None:
        """A sound by name: a ready-made one, else one saved on the page
        ({"eq", "highpass", "mono"}); None if there's no such thing."""
        if not name:
            return None
        for preset in PRESETS:
            if preset.lower() == name.lower():
                return {"name": preset, "eq": preset_bands(preset)}
        if self.config_file is not None:
            try:
                from sleepradiopi.config.settings import load
                for s in load(self.config_file).speaker_sounds:
                    if s.get("name", "").lower() == name.lower():
                        return s
            except Exception:
                log.exception("EQ mode: couldn't read the saved sounds")
        return None

    def try_sound(self, sound: dict) -> None:
        """Hear a sound now, without saving it (persist_sound does, later)."""
        self.set_eq(sound["eq"], save=False)
        if "highpass" in sound:
            self.set_highpass(sound["highpass"], save=False)
        if "mono" in sound:
            self.set_mono(bool(sound["mono"]), save=False)

    def persist_sound(self) -> None:
        """Save the EQ, low cut and mono as they are now (once, leaving EQ mode)."""
        eq = self.speaker.eq
        if eq is None:
            return
        self._save_setting("speaker_eq", dict(eq.gains))
        self._save_setting("speaker_highpass_hz", eq.highpass)
        self._save_setting("speaker_mono", bool(self.speaker.mono))

    # --- the sound by what's playing ----------------------------------------------------------

    def _by_hand(self) -> None:
        """The listener changed the sound (page, knob-side EQ mode...): it's theirs now, and holds
        until the kind of audio changes."""
        if not getattr(self._mine, "on", False):
            self._auto_on = False
            self._explicit = False
            self._baseline = None

    def set_eq_auto(self, on: bool, by_type: dict | None = None) -> None:
        """Switch the sound-by-audio-type on or off and/or change which sound each type has. Saved.
        Takes effect at once for what's playing now."""
        if by_type is not None:
            self.eq_by_type = validate_by_type(by_type)
            self._save_setting("eq_by_type", self.eq_by_type)
        if on != self.eq_auto:
            self.eq_auto = bool(on)
            self._save_setting("eq_auto", self.eq_auto)
        current, self.audio_type = self.audio_type, None
        if not self.eq_auto:
            self._leave_auto()
        elif current is not None:
            self.set_audio_type(current)
            return
        self.audio_type = current

    def _leave_auto(self) -> None:
        """Back to the listener's own sound if a type's sound is on."""
        if self._auto_on and self._baseline is not None:
            self._mine.on = True
            try:
                self.try_sound(self._baseline)
            finally:
                self._mine.on = False
        self._auto_on = False
        self._baseline = None

    def _now_sound(self) -> dict | None:
        eq = self.speaker.eq
        if eq is None:
            return None
        return {"eq": dict(eq.gains), "highpass": eq.highpass, "mono": bool(self.speaker.mono)}

    def set_audio_type(self, kind: str | None) -> None:
        """What's playing changed kind (music, radio, audiobook, ebook, podcast): give it its sound.
        A kind with no sound gets the listener's own back if a type's sound was on. Heard at once and
        never saved (the config keeps the listener's own sound, so a restart starts with it)."""
        if kind == self.audio_type:
            return
        self.audio_type = kind
        self._explicit = False            # a new kind: the kind's rule, not the last button's sound
        if not self.eq_auto or self.speaker.eq is None or kind is None:
            return
        name = self.eq_by_type.get(kind)
        sound = self.find_sound(name) if name else None
        if name and sound is None:
            log.info("sound for %s: no sound called %r", kind, name)
        try:
            if sound is not None:
                if not self._auto_on:
                    self._baseline = self._now_sound()
                self._mine.on = True
                try:
                    self.try_sound(sound)
                finally:
                    self._mine.on = False
                self._auto_on = True
                log.info("sound for %s: %s", kind, sound["name"])
            elif self._auto_on:
                self._leave_auto()
                log.info("sound for %s: back to yours", kind)
        except Exception:
            log.exception("sound for %s", kind)

    def set_button_sound(self, name: str | None, kind: str) -> None:
        """A button was pressed: name is the sound on it (None = Automatic), kind what it plays.
        A sound on the button is heard now, whatever was on, and the kind's rule steps aside until the
        kind changes or the listener changes it. A button with none goes back to the kind's rule if the
        last button's sound was on. Never saved."""
        if not self.eq_auto or self.speaker.eq is None:
            return
        sound = self.find_sound(name) if name else None
        if name and sound is None:
            log.info("button sound: no sound called %r", name)
        if sound is not None:
            if not self._auto_on:
                self._baseline = self._now_sound()
            self._mine.on = True
            try:
                self.try_sound(sound)
            finally:
                self._mine.on = False
            self._auto_on = True
            self._explicit = True
            self.audio_type = kind
            log.info("button sound: %s", sound["name"])
        elif self._explicit:
            self.audio_type = None
            self.set_audio_type(kind)

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

    @property
    def paused(self) -> bool:
        """Has the radio been paused (by a person, or the sleep timer)? While a browser has the radio instead
        of the speaker, that's whether the listening there is paused -- the speaker is quiet either way."""
        e = self.elsewhere
        return e.held if e is not None and e.on else self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)

    def play(self) -> None:
        self.slept = False
        e = self.elsewhere
        if e is not None and e.on:   # a browser has the radio: it's there that it plays again
            e.resume()
            return
        self._play()

    def pause(self) -> None:
        self.set_sleep(0)            # a pause (knob, page or the timer itself) ends the timer
        self.stop_test()
        e = self.elsewhere
        if e is not None and e.on:   # ...and there that it stops: the speaker doesn't come on
            e.hold()
            return
        self._pause()

    def _play(self) -> None:
        """The speaker itself on."""
        with self._lock:
            if not self._paused:
                return
            self._paused = False
            self.speaker.set_enabled(True)
        log.info("speaker: play")
        self._join()

    def _pause(self) -> None:
        """The speaker itself off."""
        with self._lock:
            if self._paused:
                return
            self._paused = True
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
        of the show. Paused, it plays on its own and the radio stays paused."""
        self.speaker.play_test(clip)

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
        from sleepradiopi.audio import compressor
        status = {"volume": self.speaker.volume, "playing": not self.paused, "knob_mode": self.knob_mode, "evenness": compressor.MODE,
                  "mono": self.speaker.mono, "sleep_min": None, "sleep_left_s": None, "slept": self.slept,
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
            status["eq_slots"] = list(self.eq_slots)
            status["eq_auto"] = self.eq_auto
            status["eq_by_type"] = dict(self.eq_by_type)
            status["audio_type"] = self.audio_type
            status["eq_from_type"] = self._auto_on
            status["highpass_hz"] = self.speaker.eq.highpass
        return status
