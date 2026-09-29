"""Preset buttons: four buttons on the case, like a car radio's.

A short press plays what the button holds -- the show (all artists, one
artist or one of your lists), an internet radio station, or an album
straight through -- or does an action: say the time, read the news now,
the sleep timer, or say the radio's address. Pressing the button that's
already playing pauses, and again plays. Holding a button for LONG_PRESS_S
stores whatever is playing now in it: a beep, then the DJ says "Button two:
BBC Radio 4". The web page sets them too (Streaming -> Buttons).

The kernel does the GPIO work (config.txt): gpio-key overlays sending
KEY_1..KEY_4, read with the knob's input events (io/knob.py).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.config.settings import save_setting
from sleepradiopi.io.announce import Clip, beep
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

N = 4
KEYCODES = {2: 0, 3: 1, 4: 2, 5: 3}          # KEY_1..KEY_4 -> button index
WORDS = ("one", "two", "three", "four")
ACTIONS = {"time": "Say the time", "news": "The news now", "sleep": "Sleep timer (30 min)",
           "address": "Say the address", "noise": "Noise on/off"}
SLEEP_MIN = 30
MAX_TEXT = 200


def _text(value, name: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT or (required and not value.strip()):
        raise ValueError(f"a button's {name} must be text (up to {MAX_TEXT} letters)")
    return value.strip() or None


def validate(preset) -> dict | None:
    """One button's preset (None = empty) -> a clean copy; ValueError if wrong."""
    if preset is None:
        return None
    if not isinstance(preset, dict):
        raise ValueError("a button holds a preset or null")
    kind = preset.get("kind")
    if kind == "show":
        artist, profile = _text(preset.get("artist"), "artist"), _text(preset.get("profile"), "list")
        return {"kind": "show", "artist": None if profile else artist, "profile": profile}
    if kind == "radio":
        return {"kind": "radio", **radio_mod.validate_station(preset)}
    if kind == "album":
        return {"kind": "album", "folder": _text(preset.get("folder"), "album folder", True),
                "title": _text(preset.get("title"), "album title") or "",
                "artist": _text(preset.get("artist"), "artist") or ""}
    if kind == "podcast":
        return {"kind": "podcast", "show": _text(preset.get("show"), "podcast", True),
                "title": _text(preset.get("title"), "podcast title") or "",
                "start": _text(preset.get("start"), "starting episode"),        # its guid; None = latest / part-heard
                "start_title": _text(preset.get("start_title"), "starting episode's title") or ""}
    if kind == "book":
        return {"kind": "book", "key": _text(preset.get("key"), "book", True),
                "title": _text(preset.get("title"), "book title") or ""}
    if kind == "action":
        if preset.get("action") not in ACTIONS:
            raise ValueError(f"a button's action is one of {', '.join(ACTIONS)}")
        return {"kind": "action", "action": preset["action"]}
    raise ValueError("a button holds the show, a radio station, an album, an audiobook, a podcast or an action")


def validate_all(presets) -> list:
    """The four buttons (a shorter list is padded with empty ones)."""
    if not isinstance(presets, list) or len(presets) > N:
        raise ValueError(f"send a list of up to {N} buttons")
    return [validate(p) for p in presets] + [None] * (N - len(presets))


def label(preset: dict | None, station_name: Callable[[dict], str] | None = None) -> str:
    """What a button is, in words: "BBC Radio 4", "Rubber Soul — The Beatles"."""
    if preset is None:
        return "Empty"
    kind = preset["kind"]
    if kind == "radio":
        return preset["name"]
    if kind == "album":
        return f"{preset['title']} — {preset['artist']}" if preset["artist"] else preset["title"]
    if kind == "book":
        return preset["title"] or preset["key"]
    if kind == "podcast":
        return preset["title"] or "Podcast"
    if kind == "action":
        return ACTIONS[preset["action"]]
    if station_name is not None:
        return station_name(preset)
    return preset["profile"] or (f"{preset['artist']} Radio" if preset["artist"] else "Sleep Radio")


def same(a: dict | None, b: dict | None) -> bool:
    """Do two presets play the same thing?"""
    if a is None or b is None or a["kind"] != b["kind"]:
        return False
    if a["kind"] == "radio":
        return a["url"] == b["url"]
    if a["kind"] == "album":
        return a["folder"] == b["folder"]
    if a["kind"] == "book":
        return a["key"] == b["key"]
    if a["kind"] == "podcast":
        return a["show"] == b["show"]
    if a["kind"] == "show":
        return ((a["profile"] or "").lower(), (a["artist"] or "").lower()) == \
               ((b["profile"] or "").lower(), (b["artist"] or "").lower())
    return a == b


class Presets:
    """The buttons' presets, and what a press or a hold does."""

    def __init__(self, station, control=None, config_file: Path | None = None,
                 presets: list | None = None, announcer=None) -> None:
        self.station = station
        self.control = control           # SpeakerControl: play/pause, sleep timer, clips
        self.config_file = config_file
        self.announcer = announcer       # says the address
        try:
            self.presets = validate_all(presets or [])
        except ValueError as e:          # a hand-edited config: don't stop the station
            log.warning("buttons ignored: %s", e)
            self.presets = [None] * N
        self._lock = threading.Lock()

    # --- what's playing -----------------------------------------------------------------

    def current(self) -> dict:
        """What's playing now, as a preset."""
        src = self.station.source
        if src is not None and src["kind"] == "radio":
            return validate({k: v for k, v in src.items() if k in ("kind", "name", "url", "info")})
        if src is not None and src["kind"] == "episode":         # (the episode playing starts the sequence)
            from sleepradiopi.playback.podcasts import guid_id
            return validate({"kind": "podcast", "show": src["show"], "title": src.get("show_title", ""),
                             "start": guid_id(src["guid"]), "start_title": src.get("title", "")[:MAX_TEXT]})
        if src is not None and src["kind"] == "book":
            return validate({"kind": "book", "key": src["key"], "title": src.get("title", "")})
        if src is not None and src["kind"] == "album":
            return validate({"kind": "album", "folder": src["folder"], "title": src["title"],
                             "artist": src["artist"]})
        return validate({"kind": "show", "artist": self.station.artist, "profile": self.station.profile})

    def label(self, preset: dict | None) -> str:
        return label(preset, self._show_name)

    def _show_name(self, preset: dict) -> str:
        from sleepradiopi.broadcast import profiles as profiles_mod
        from sleepradiopi.broadcast.script_builder import artist_station_name
        if preset["profile"]:
            return profiles_mod.station_name(preset["profile"])
        return artist_station_name(preset["artist"])

    def status(self) -> dict:
        now = self.current()
        return {"buttons": [{"preset": p, "label": self.label(p), "playing": same(p, now)}
                            for p in self.presets],
                "now": {"preset": now, "label": self.label(now)}, "actions": ACTIONS}

    # --- setting them -------------------------------------------------------------------

    def set(self, index: int, preset) -> dict | None:
        """Put a preset on a button (None empties it). Saved."""
        if not 0 <= index < N:
            raise ValueError(f"buttons are 1 to {N}")
        preset = validate(preset)
        with self._lock:
            self.presets[index] = preset
            self._save()
        log.info("button %d: %s", index + 1, self.label(preset))
        return preset

    def set_all(self, presets: list) -> None:
        presets = validate_all(presets)
        with self._lock:
            self.presets = presets
            self._save()

    def _save(self) -> None:
        if self.config_file is not None:
            save_setting(self.config_file, "buttons", self.presets)

    # --- the buttons -----------------------------------------------------------------------

    def press(self, index: int) -> None:
        """A short press: play the button's preset, or pause/play if it's what's
        playing already; do its action."""
        preset = self.presets[index]
        log.info("button %d pressed: %s", index + 1, self.label(preset))
        if preset is None:
            self._say(f"Button {WORDS[index]} is empty. Hold it down to keep what's playing on it.")
            return
        if preset["kind"] == "action":
            self._action(preset["action"])
            return
        if same(preset, self.current()) and self.control is not None:
            self.control.toggle()
            return
        try:
            self.apply(preset)
        except ValueError as e:
            log.warning("button %d: %s", index + 1, e)
            self._say(f"Sorry, I can't play {self.label(preset)}.")
            return
        if self.control is not None:
            self.control.play()

    def hold(self, index: int) -> None:
        """A long press: keep what's playing now on this button."""
        preset = self.set(index, self.current())
        self._say(f"Button {WORDS[index]}: {self.label(preset)}.")

    def apply(self, preset: dict) -> None:
        """Play a (non-action) preset. ValueError if it can't be played here."""
        if preset["kind"] == "show":
            if preset["profile"]:                # the choice first, so the show starts with it
                self.station.set_profile(preset["profile"])
            else:
                self.station.set_artist(preset["artist"])
            if self.station.source is not None:
                self.station.tune(None)
            if self.config_file is not None:
                save_setting(self.config_file, "broadcast_artist", self.station.artist)
                save_setting(self.config_file, "broadcast_profile", self.station.profile)
        elif preset["kind"] in ("radio", "album", "book"):
            self.station.tune(preset)
        elif preset["kind"] == "podcast":
            self.station.play_episode(preset["show"], start=preset.get("start"))   # on through, in order
        else:
            raise ValueError("not something to play")

    # --- actions and speech -------------------------------------------------------------------

    def _action(self, action: str) -> None:
        if action == "time":
            text = (self.station.builder.time_line() if clock_trusted()
                    else "Sorry, I don't know the time yet.")
            self._say(text)
        elif action == "news":
            self._clip(beep(), "Button")
            threading.Thread(target=self._news, name="news-now", daemon=True).start()
        elif action == "sleep" and self.control is not None:
            on = not self.control.status().get("sleep_min")
            self.control.set_sleep(SLEEP_MIN if on else 0)
            self._say(f"Sleep timer, {SLEEP_MIN} minutes." if on else "Sleep timer off.")
        elif action == "address" and self.announcer is not None:
            self.announcer.speak()
        elif action == "noise" and self.control is not None:
            self.control.toggle_noise()          # (the noise itself says it's on: no beep)

    def _news(self) -> None:
        try:
            audio = self.station.news_now()
        except Exception:
            log.exception("news now failed")
            audio = None
        if audio is None:
            self._say("Sorry, there's no news to read. Is the radio online?")
        else:
            self._clip(audio, "News")

    def _clip(self, audio: np.ndarray, label_: str) -> None:
        if self.control is not None:
            self.control.play_clip(Clip(audio, "button", label_))

    def _say(self, text: str) -> None:
        """A beep at once, then the line in the DJ's voice (made in the background:
        slow on a Zero). Just the beep without a voice."""
        log.info("button: %s", text)
        self._clip(beep(), "Button")
        if not getattr(self.station, "_has_voice", False) or self.control is None:
            return

        def run():
            try:
                self._clip(self.station.render_speech(text), "Button")
            except Exception:
                log.exception("button: couldn't say it")
        threading.Thread(target=run, name="button-say", daemon=True).start()
