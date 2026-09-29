"""Preset buttons: four buttons on the case, like a car radio's.

A short press plays what the button holds -- the show (all artists, one
artist or one of your lists), an internet radio station, or an album
straight through -- or does an action: say the time, read the news now,
the sleep timer, or say the radio's address. Pressing the button that's
already playing pauses, and again plays. Holding a button for LONG_PRESS_S
stores whatever is playing now in it: a beep, then the DJ says "Button two:
BBC Radio 4". The web page sets them too (Streaming -> Buttons).

There are two sets of the four, **day** and **night**, each with its own
presets; everything (the real buttons, the page's) uses the one in use.
Holding buttons 2 and 3 together for BANK_HOLD_S swaps them (it ticks while
held, then a rising or falling two-note sound, and the DJ says which), the
page has a switch, and an optional timetable swaps them by the clock
(`buttons_auto`: night from night_min, day from day_min); a swap by hand
lasts until the next time on the timetable.

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
           "address": "Say the address", "noise": "Noise on/off", "dj": "DJ on/off"}
BANKS = ("day", "night")
BANK_KEYS = (3, 4)          # buttons 2 and 3...
BANK_HOLD_S = 3.0           # ...held together this long swap day and night
AUTO_DEFAULT = {"on": False, "night_min": 21 * 60, "day_min": 7 * 60}
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


def validate_auto(auto) -> dict:
    """The day/night timetable: {"on", "night_min", "day_min"} (minutes after midnight)."""
    if auto is None:
        auto = {}
    if not isinstance(auto, dict):
        raise ValueError("buttons_auto must be an object")
    out = {**AUTO_DEFAULT, **{k: v for k, v in auto.items() if k in AUTO_DEFAULT}}
    if not isinstance(out["on"], bool):
        raise ValueError("buttons_auto on is true or false")
    for k in ("night_min", "day_min"):
        if isinstance(out[k], bool) or not isinstance(out[k], int) or not 0 <= out[k] < 24 * 60:
            raise ValueError(f"buttons_auto {k} is a time of day (minutes)")
    if out["night_min"] == out["day_min"]:
        raise ValueError("night and day can't start at the same time")
    return out


def scheduled_bank(auto: dict, minute: int) -> str | None:
    """Which set the timetable wants at this minute of the day (None: it's off)."""
    if not auto["on"]:
        return None
    n, d = auto["night_min"], auto["day_min"]
    night = (minute >= n or minute < d) if n > d else (n <= minute < d)
    return "night" if night else "day"


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
                 presets: list | None = None, announcer=None, night: list | None = None,
                 bank: str = "day", auto: dict | None = None) -> None:
        self.station = station
        self.control = control           # SpeakerControl: play/pause, sleep timer, clips
        self.menu = None                 # the service menu (io/service.py): gets the presses while it's open
        self.keys = None                 # the knob's key handlers {keycode: (down, up)}: the page's buttons use them too
        self.config_file = config_file
        self.announcer = announcer       # says the address
        self.banks = {"day": self._safe(presets), "night": self._safe(night)}
        self.bank = bank if bank in BANKS else "day"
        try:
            self.auto = validate_auto(auto)
        except ValueError as e:
            log.warning("buttons timetable ignored: %s", e)
            self.auto = dict(AUTO_DEFAULT)
        self._auto_last: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _safe(presets) -> list:
        try:
            return validate_all(presets or [])
        except ValueError as e:          # a hand-edited config: don't stop the station
            log.warning("buttons ignored: %s", e)
            return [None] * N

    @property
    def presets(self) -> list:
        """The four in the set in use."""
        return self.banks[self.bank]

    @presets.setter
    def presets(self, value: list) -> None:
        self.banks[self.bank] = value

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
                "now": {"preset": now, "label": self.label(now)}, "actions": ACTIONS,
                "bank": self.bank, "auto": self.auto,
                "service": self.menu.state if self.menu is not None else None}

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

    def load_all(self, day=None, night=None, bank=None, auto=None) -> None:
        """From a loaded settings file (any of them may be missing)."""
        with self._lock:
            if day is not None:
                self.banks["day"] = validate_all(day)
            if night is not None:
                self.banks["night"] = validate_all(night)
            if bank in BANKS:
                self.bank = bank
            if auto is not None:
                self.auto = validate_auto(auto)
                self._auto_last = None
            self._save()

    def _save(self) -> None:
        if self.config_file is not None:
            save_setting(self.config_file, "buttons", self.banks["day"])
            save_setting(self.config_file, "buttons_night", self.banks["night"])
            save_setting(self.config_file, "buttons_bank", self.bank)

    # --- day and night ------------------------------------------------------------------

    def set_bank(self, bank: str, announce: bool = True) -> str:
        """Use the day or night set. announce: the two-note sound and the DJ saying so."""
        if bank not in BANKS:
            raise ValueError("the buttons' set is day or night")
        with self._lock:
            changed = bank != self.bank
            self.bank = bank
            if changed:
                self._save()
        log.info("buttons: the %s set%s", bank, "" if changed else " (already)")
        if announce:
            tones = (660.0, 990.0) if bank == "day" else (990.0, 660.0)   # rising for day, falling for night
            self._clip(beep(tones, 0.16), "Button")
            self._say(f"{bank.capitalize()} buttons.", beep_first=False)
        return bank

    def toggle_bank(self) -> str:
        if self.menu is not None and self.menu.active:
            return self.bank                  # (the service menu has the buttons)
        return self.set_bank("night" if self.bank == "day" else "day")

    def set_auto(self, auto: dict) -> dict:
        """The timetable (validated; saved). It takes effect at once."""
        auto = validate_auto(auto)
        with self._lock:
            self.auto = auto
            self._auto_last = None
        if self.config_file is not None:
            save_setting(self.config_file, "buttons_auto", auto)
        self.check_auto()
        return auto

    def check_auto(self, now_minute: int | None = None) -> None:
        """Swap sets when the timetable passes a switch time (quietly: it may be
        the middle of the night). A swap by hand lasts until the next one."""
        if now_minute is None:
            if not clock_trusted():
                return
            import time as _time
            t = _time.localtime()
            now_minute = t.tm_hour * 60 + t.tm_min
        want = scheduled_bank(self.auto, now_minute)
        if want is None:
            self._auto_last = None
            return
        if want != self._auto_last:
            self._auto_last = want
            if want != self.bank:
                self.set_bank(want, announce=False)
                log.info("buttons: timetable swapped to the %s set", want)

    def keep_auto(self, every_s: float = 20.0) -> None:
        def run():
            import time as _time
            while True:
                try:
                    self.check_auto()
                except Exception:
                    log.exception("buttons timetable")
                _time.sleep(every_s)
        threading.Thread(target=run, name="buttons-auto", daemon=True).start()

    # --- the buttons -----------------------------------------------------------------------

    def press(self, index: int) -> None:
        """A short press: play the button's preset, or pause/play if it's what's
        playing already; do its action."""
        if self.menu is not None and self.menu.active:
            self.menu.press(index)
            return
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
        if self.menu is not None and self.menu.active:
            self.menu.press(index)
            return
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
            def tell():
                tts = getattr(self.station, "tts", None)
                if tts is not None and hasattr(tts, "wake"):
                    tts.wake()                    # (asleep: load it first, so the time is right when said)
                self._say(self.station.builder.time_line() if clock_trusted()
                          else "Sorry, I don't know the time yet.")
            self._clip(beep(), "Button")          # (at once: the voice may need a moment to wake)
            threading.Thread(target=tell, name="button-time", daemon=True).start()
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
        elif action == "dj":
            on = not self.station.dj_on
            self.station.set_dj(dj_on=on)
            if self.config_file is not None:
                save_setting(self.config_file, "broadcast_dj", on)
            self._say("DJ on." if on else "DJ off. Just the music.")

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

    def _say(self, text: str, beep_first: bool = True) -> None:
        """A beep at once, then the line in the DJ's voice (made in the background:
        slow on a Zero). Just the beep without a voice."""
        log.info("button: %s", text)
        if beep_first:
            self._clip(beep(), "Button")
        if not getattr(self.station, "_has_voice", False) or self.control is None:
            return

        def run():
            try:
                self._clip(self.station.render_speech(text), "Button")
            except Exception:
                log.exception("button: couldn't say it")
        threading.Thread(target=run, name="button-say", daemon=True).start()
