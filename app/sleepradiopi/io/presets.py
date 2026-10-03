"""Preset buttons: four buttons on the case, like a car radio's.

A short press plays what the button holds -- the show (all artists, one
artist or one of your lists), an internet radio station, or an album
straight through -- or does an action: say the time, read the news now,
the sleep timer, or say the radio's address. A button only ever plays:
pausing is the knob's job. The desktop page sets them (drag things onto a
button, or the Buttons window); there's no hold-to-store on the case.

A button can hold several **steps** (up to MAX_STEPS): stations on one,
podcasts on the next, audiobooks on a third. Pressing it again moves on a
step, round and round. It waits STEP_SETTLE_S after the last press before
playing (so stepping past a book doesn't move its place), then says the
step's name -- made ahead of time (io/button_names.py), so there's nothing
to wait for -- or, until that's ready (or with the DJ off), gives one pip
for step one, two for step two... Coming back to a button from something
else, it picks up at the step it was last on. (The cathedral's selector has
no second press: it plays a position's first step.)

There are two sets of the four, **day** and **night**, each with its own
presets; everything (the real buttons, the page's) uses the one in use.
Holding buttons 2 and 3 together for BANK_HOLD_S swaps them (it ticks while
held, then three notes: rising for day, falling for night), the
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
from sleepradiopi.io.announce import Clip, beep, pip, pips
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

N = 4                                         # the box radio's buttons
MAX_N = 6                                     # the cathedral's selector positions
KEYCODES = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}   # KEY_1..KEY_6 -> preset index
BACK_KEY = 139                                # KEY_MENU: the cathedral's hidden back button
WORDS = ("one", "two", "three", "four", "five", "six")
SETTLE_S = 0.6            # the selector: a position counts once it's been there this long (turning
                          # from 1 to 4 passes 2 and 3 without playing them)
BACK_HOLD_S = 5.0         # the back button held this long: the service menu
INSTANT = ("action", "message", "jingle", "birthday")     # done at once over what's on: nothing to pause
ACTIONS = {"time": "Say the time", "news": "The news now", "sleep": "Sleep timer (30 min)",
           "address": "Say the address", "noise": "Noise on/off", "dj": "DJ on/off", "pips": "The pips"}
BANKS = ("day", "night")
BANK_KEYS = (3, 4)          # buttons 2 and 3...
BANK_HOLD_S = 1.0           # ...held together this long swap day and night
AUTO_DEFAULT = {"on": False, "night_min": 21 * 60, "day_min": 7 * 60}
SLEEP_MIN = 30
MAX_TEXT = 200
MAX_STEPS = 6               # what one button can hold, stepped through by pressing it again
STEP_SETTLE_S = 0.8         # a button with steps plays this long after its last press
STEP_RESET_S = 30.0         # an action step done this long ago: the next press isn't "the one after it"


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
        out = {"kind": "album", "folder": _text(preset.get("folder"), "album folder", True),
               "title": _text(preset.get("title"), "album title") or "",
               "artist": _text(preset.get("artist"), "artist") or ""}
        if preset.get("root", "music") not in ("music", "ondemand"):
            raise ValueError("an album is in music or ondemand")
        if preset.get("root") == "ondemand":
            out["root"] = "ondemand"
        if preset.get("deep") is True:
            out["deep"] = True
        return out
    if kind == "programme":
        return {"kind": "programme", "name": _text(preset.get("name"), "programme", True)}
    if kind == "playlist":
        return {"kind": "playlist", "name": _text(preset.get("name"), "playlist", True),
                "shuffle": preset.get("shuffle") is True}
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
    if kind == "message":                                     # (instants: said or played over what's on)
        text = preset.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("a message button needs its words")
        return {"kind": "message", "text": " ".join(text.split())}
    if kind == "jingle":
        path = preset.get("path")
        if not isinstance(path, str) or not path.strip("/") or len(path) > 1000 or ".." in path.split("/"):
            raise ValueError("a jingle button needs its file")
        return {"kind": "jingle", "path": path.strip("/")}
    if kind == "birthday":
        from sleepradiopi.broadcast import birthdays
        return {"kind": "birthday", **birthdays.validate([{k: preset.get(k) for k in ("name", "day", "month", "year") if preset.get(k) is not None}])[0]}
    raise ValueError("a button holds the show, a radio station, an album, a playlist, a programme, an audiobook, a podcast, "
                     "an action, a message, a jingle or a birthday")


def steps(button) -> list:
    """A button's steps, in order: [] when it's empty."""
    if button is None:
        return []
    return list(button) if isinstance(button, list) else [button]


def validate_button(button):
    """What one button holds -> a clean copy: None (empty), a preset, or a list of
    two to MAX_STEPS presets (its steps). A list of one is just that preset, so
    a button with one thing on it is saved as it always was."""
    if not isinstance(button, list):
        return validate(button)
    out = [p for p in (validate(p) for p in button) if p is not None]
    if len(out) > MAX_STEPS:
        raise ValueError(f"a button holds up to {MAX_STEPS} steps")
    return out if len(out) > 1 else (out[0] if out else None)


def step_pips(n: int) -> np.ndarray:
    """Which step a button landed on: n short pips, int16 stereo."""
    from sleepradiopi.audio import pcm
    parts = []
    for _ in range(max(1, n)):
        parts += [pip(0.07), pcm.silence(0.11)]
    return np.concatenate(parts[:-1])


def validate_all(presets, n: int = N) -> list:
    """The buttons (a shorter list is padded with empty ones; a longer one, up
    to MAX_N, is kept -- e.g. a cathedral's six, on a box's four)."""
    if not isinstance(presets, list) or len(presets) > MAX_N:
        raise ValueError(f"send a list of up to {MAX_N} buttons")
    out = [validate_button(p) for p in presets]
    return (out + [None] * (n - len(out)))[:max(n, len(out))]


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
    if kind == "playlist":
        return preset["name"] + (" (shuffled)" if preset["shuffle"] else "")
    if kind == "programme":
        return preset["name"]
    if kind == "podcast":
        return preset["title"] or "Podcast"
    if kind == "action":
        return ACTIONS[preset["action"]]
    if kind == "message":
        return "“" + (preset["text"] if len(preset["text"]) <= 30 else preset["text"][:28] + "…") + "”"
    if kind == "jingle":
        return Path(preset["path"]).stem
    if kind == "birthday":
        return f"{preset['name']}'s birthday"
    if station_name is not None:
        return station_name(preset)
    from sleepradiopi.config import brand
    return preset["profile"] or (f"{preset['artist']} Radio" if preset["artist"] else brand.name)


def spoken(preset: dict, name: str) -> str:
    """A step's name as it's said when a button lands on it (name: its label)."""
    if preset["kind"] == "playlist":
        return preset["name"]
    return name.replace(" — ", ", ")


def same(a: dict | None, b: dict | None) -> bool:
    """Do two presets play the same thing?"""
    if a is None or b is None or a["kind"] != b["kind"]:
        return False
    if a["kind"] == "radio":
        return a["url"] == b["url"]
    if a["kind"] == "album":
        return (a["folder"], a.get("root", "music"), a.get("deep", False)) == \
            (b["folder"], b.get("root", "music"), b.get("deep", False))
    if a["kind"] == "book":
        return a["key"] == b["key"]
    if a["kind"] == "playlist":
        return (a["name"].lower(), a["shuffle"]) == (b["name"].lower(), b["shuffle"])
    if a["kind"] == "programme":
        return a["name"].lower() == b["name"].lower()
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
                 bank: str = "day", auto: dict | None = None, count: int = N, selector: bool = False) -> None:
        """count: how many presets (4 buttons on the box, 6 on the cathedral's
        selector); selector: they're positions of a rotary switch, not buttons --
        landing on one plays it (after SETTLE_S), and there's no hold-to-save."""
        self.count, self.selector = count, selector
        self._settle: threading.Timer | None = None
        self.station = station
        self.control = control           # SpeakerControl: play/pause, sleep timer, clips
        self.jingle: Callable[[str], None] | None = None     # plays a jingle file over what's on (main sets it)
        self.menu = None                 # the service menu (io/service.py): gets the presses while it's open
        self.keys = None                 # the knob's key handlers {keycode: (down, up)}: the page's buttons use them too
        self.config_file = config_file
        self.announcer = announcer       # says the address
        self.names = None                # the steps' spoken names, made ahead (io/button_names.py; main sets it)
        self.banks = {"day": self._safe(presets, count), "night": self._safe(night, count)}
        self.bank = bank if bank in BANKS else "day"
        try:
            self.auto = validate_auto(auto)
        except ValueError as e:
            log.warning("buttons timetable ignored: %s", e)
            self.auto = dict(AUTO_DEFAULT)
        self._auto_last: str | None = None
        self._lock = threading.Lock()
        # a button with steps: the press waiting to settle (button, step, its timer), the step
        # each button was last on {(set, button): step}, and the last one done (set, button, step, when)
        self._pending: tuple | None = None
        self._at: dict = {}
        self._last: tuple | None = None

    @staticmethod
    def _safe(presets, n: int = N) -> list:
        try:
            return validate_all(presets or [], n)
        except ValueError as e:          # a hand-edited config: don't stop the station
            log.warning("buttons ignored: %s", e)
            return [None] * n

    @property
    def presets(self) -> list:
        """The four in the set in use."""
        return self.banks[self.bank]

    @presets.setter
    def presets(self, value: list) -> None:
        self.banks[self.bank] = value

    def _every(self):
        """Every preset on every button, in both sets (each step of the ones with steps)."""
        for bank in self.banks.values():
            for button in bank:
                yield from steps(button)

    def pin_shows(self, theme: str) -> bool:
        """There's no default station: buttons set to "the show" (no theme) play this
        theme instead."""
        changed = False
        for p in self._every():
            if p["kind"] == "show" and not p.get("profile") and not p.get("artist"):
                p["profile"] = theme
                changed = True
        if changed:
            self._save()
        return changed

    def artists_to_themes(self, theme_for) -> bool:
        """Artist radio is retired: a button that played one artist plays that
        artist's one-artist theme (theme_for makes it; Default if they've gone)."""
        changed = False
        for p in self._every():
            if p["kind"] == "show" and not p.get("profile") and p.get("artist"):
                p["profile"], p["artist"] = theme_for(p["artist"]) or "Default", None
                changed = True
        if changed:
            self._save()
        return changed

    def rename_theme(self, old: str, new: str) -> bool:
        """A theme was renamed: buttons that play it follow it."""
        changed = False
        for p in self._every():
            if p["kind"] == "show" and (p.get("profile") or "").lower() == old.lower():
                p["profile"] = new
                changed = True
        if changed:
            self._save()
        return changed

    def rename_playlist(self, old: str, new: str) -> bool:
        """A playlist was renamed: buttons that play it follow it."""
        changed = False
        for p in self._every():
            if p["kind"] == "playlist" and p["name"].lower() == old.lower():
                p["name"] = new
                changed = True
        if changed:
            self._save()
        return changed

    def rename_refs(self, old_root: str, old: str, new_root: str, new: str) -> bool:
        """A folder was moved: album buttons that held it (or something in it) follow it."""
        changed = False
        for p in self._every():
            if p["kind"] == "album" and p.get("root", "music") == old_root and (p["folder"] == old or p["folder"].startswith(old + "/")):
                p["folder"] = new + p["folder"][len(old):]
                if new_root == "music":
                    p.pop("root", None)
                else:
                    p["root"] = new_root
                changed = True
        if changed:
            self._save()
        return changed

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
        if src is not None and src["kind"] == "playlist":
            return validate({"kind": "playlist", "name": src["name"], "shuffle": src.get("shuffle", False)})
        if src is not None and src["kind"] == "album":
            return validate({"kind": "album", "folder": src["folder"], "title": src["title"],
                             "artist": src["artist"], "root": src.get("root", "music"),
                             "deep": src.get("deep", False)})
        sched = getattr(self.station, "scheduler", None)
        if sched is not None and sched.run is not None:
            return validate({"kind": "programme", "name": sched.run["name"]})
        pl = self.station.playlist_status() if hasattr(self.station, "playlist_status") else None
        if src is None and pl is not None:
            return validate({"kind": "playlist", "name": pl["name"], "shuffle": pl["shuffle"]})
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
        return {"buttons": [self._button(b, now) for b in self.presets[:self.count]],
                "count": self.count, "selector": self.selector, "max_steps": MAX_STEPS,
                "now": {"preset": now, "label": self.label(now)}, "actions": ACTIONS,
                "bank": self.bank, "auto": self.auto,
                "sets": {b: [self._button(x, now if b == self.bank else None) for x in self.banks[b][:self.count]]
                         for b in BANKS},
                "service": self.menu.state if self.menu is not None else None}

    def _button(self, button, now: dict | None) -> dict:
        """A button for the page: its steps, and (as "preset" and "label") the one
        that's playing -- or, with none playing, its first."""
        rows = [{"preset": p, "label": self.label(p), "playing": same(p, now)} for p in steps(button)]
        if self.names is not None and len(rows) > 1:
            for r in rows:                   # (said when the button lands on it: is its name made yet?)
                if r["preset"]["kind"] not in INSTANT:
                    r["named"] = self.names.has(spoken(r["preset"], r["label"]))
        on = next((k for k, r in enumerate(rows) if r["playing"]), None)
        shown = rows[on or 0] if rows else {"preset": None, "label": self.label(None)}
        return {"preset": shown["preset"], "label": shown["label"], "playing": on is not None,
                "steps": rows, "step": on}

    # --- setting them -------------------------------------------------------------------

    def set(self, index: int, preset, bank: str | None = None):
        """Put a preset on a button (None empties it; a list: its steps), in the set
        in use or the one named (day or night: programming the other set without
        swapping). Saved."""
        if not 0 <= index < self.count:
            raise ValueError(f"buttons are 1 to {self.count}")
        if bank is not None and bank not in BANKS:
            raise ValueError("the set is day or night")
        preset = validate_button(preset)
        with self._lock:
            self.banks[bank or self.bank][index] = preset
            self._at.pop((bank or self.bank, index), None)        # (its steps changed: from the first)
            if self._pending is not None and self._pending[0] == index:
                self._pending[2].cancel()
                self._pending = None
            self._save()
        log.info("button %d%s: %s", index + 1, f" ({bank})" if bank else "",
                 " / ".join(self.label(p) for p in steps(preset)) or self.label(None))
        return preset

    def add(self, index: int, preset, bank: str | None = None):
        """One more step on the end of a button."""
        if not 0 <= index < self.count:
            raise ValueError(f"buttons are 1 to {self.count}")
        if bank is not None and bank not in BANKS:
            raise ValueError("the set is day or night")
        return self.set(index, steps(self.banks[bank or self.bank][index]) + [validate(preset)], bank)

    def set_all(self, presets: list) -> None:
        presets = validate_all(presets, self.count)
        with self._lock:
            self.presets = presets
            self._save()

    def load_all(self, day=None, night=None, bank=None, auto=None) -> None:
        """From a loaded settings file (any of them may be missing)."""
        with self._lock:
            if day is not None:
                self.banks["day"] = validate_all(day, self.count)
            if night is not None:
                self.banks["night"] = validate_all(night, self.count)
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
        self.bake()

    def bake(self) -> None:
        """Have the spoken names made for the steps that'll want one (the buttons
        with more than one thing on them; actions make their own sound)."""
        if self.names is None:
            return
        try:
            self.names.want([spoken(p, self.label(p)) for bank in self.banks.values() for button in bank
                             if isinstance(button, list) for p in button if p["kind"] not in INSTANT])
        except Exception:
            log.exception("button names")

    # --- day and night ------------------------------------------------------------------

    def set_bank(self, bank: str, announce: bool = True) -> str:
        """Use the day or night set. announce: three notes, rising for day, falling for night."""
        if bank not in BANKS:
            raise ValueError("the buttons' set is day or night")
        with self._lock:
            changed = bank != self.bank
            self.bank = bank
            if changed:
                self._save()
        log.info("buttons: the %s set%s", bank, "" if changed else " (already)")
        if announce:                          # (no words: instant, even with the voice asleep)
            notes = (523.25, 659.25, 783.99)      # C E G
            self._clip(beep(notes if bank == "day" else notes[::-1], 0.14), "Button")
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

    # --- the cathedral's selector and back button -----------------------------------------

    def selector_down(self, index: int) -> None:
        """The selector reached a position: it counts once it stays there SETTLE_S."""
        if self._settle is not None:
            self._settle.cancel()
        self._settle = threading.Timer(SETTLE_S, self._settled, args=(index,))
        self._settle.daemon = True
        self._settle.start()

    def selector_up(self, index: int) -> None:
        """The selector left a position (on its way to another)."""
        if self._settle is not None and self._settle.args == (index,):
            self._settle.cancel()
            self._settle = None

    def _settled(self, index: int) -> None:
        self._settle = None
        self.press(index)

    def back_press(self) -> None:
        """The hidden back button: in the service menu, confirm; otherwise swap day and night."""
        if self.menu is not None and self.menu.active:
            self.menu.confirm()
        else:
            self.toggle_bank()

    def back_hold(self) -> None:
        """Held for BACK_HOLD_S: the service menu."""
        if self.menu is not None and not self.menu.active:
            self.menu.open()

    def press(self, index: int, step: int | None = None) -> None:
        """A press: play what the button holds (it never pauses: that's the knob),
        or do its action. A button with steps moves on one each press, and plays
        once it's been left alone for STEP_SETTLE_S. step: that one, now (the
        page's own play keys). (The selector: a position reached.)"""
        if not 0 <= index < self.count:
            return
        if self.menu is not None and self.menu.active:
            (self.menu.select if self.selector else self.menu.press)(index)
            return
        if self.selector:
            self._choose(index)
            return
        chain = steps(self.presets[index])
        if not chain:
            log.info("button %d pressed: empty", index + 1)
            self._say(f"Button {WORDS[index]} is empty. Set it from the radio's page.")
            return
        if step is not None:
            if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step < len(chain):
                raise ValueError(f"button {index + 1} has {len(chain)} step{'' if len(chain) == 1 else 's'}")
            with self._lock:
                self._drop_pending()
            self._step(index, step, quiet=len(chain) == 1)
            return
        if len(chain) == 1:
            with self._lock:
                self._drop_pending()
            self._step(index, 0, quiet=True)
            return
        with self._lock:
            k = self._next_step(index, chain)
            self._drop_pending()
            timer = threading.Timer(STEP_SETTLE_S, self._step_settled, args=(index, k))
            timer.daemon = True
            self._pending = (index, k, timer)
            timer.start()
        log.info("button %d pressed: step %d of %d", index + 1, k + 1, len(chain))

    def _drop_pending(self) -> None:
        if self._pending is not None:
            self._pending[2].cancel()
            self._pending = None

    def _next_step(self, index: int, chain: list) -> int:
        """Which step this press of a button with steps means: the one after the
        last press (still settling), after the one that's playing, or after an
        action it has just done; otherwise -- coming back to the button from
        something else -- the step it was last on."""
        import time as _time
        n = len(chain)
        if self._pending is not None and self._pending[0] == index:
            return (self._pending[1] + 1) % n
        now, last = self.current(), self._last
        if last is not None and last[:2] == (self.bank, index) and last[2] < n \
                and chain[last[2]]["kind"] in INSTANT and _time.monotonic() - last[3] < STEP_RESET_S:
            return (last[2] + 1) % n
        on = next((k for k, p in enumerate(chain) if same(p, now)), None)
        if on is not None:
            return (on + 1) % n
        was = self._at.get((self.bank, index), 0)
        return was if was < n and chain[was]["kind"] not in INSTANT else 0

    def _step_settled(self, index: int, k: int) -> None:
        with self._lock:
            if self._pending is None or self._pending[:2] != (index, k):
                return                       # (pressed again, or its steps were changed)
            self._pending = None
        if self.menu is not None and self.menu.active:
            return
        self._step(index, k)

    def _step(self, index: int, k: int, quiet: bool = False) -> None:
        """Do a button's step k. quiet: no pips (a button with just the one thing)."""
        import time as _time
        chain = steps(self.presets[index])
        if k >= len(chain):
            return
        preset = chain[k]
        log.info("button %d%s: %s", index + 1, "" if len(chain) == 1 else f" step {k + 1}", self.label(preset))
        self._at[(self.bank, index)] = k
        self._last = (self.bank, index, k, _time.monotonic())
        if not quiet and preset["kind"] not in INSTANT:      # (an action is heard anyway: its own beep or words)
            self._clip(self._step_sound(preset, k), "Button")
        self._play(preset, f"button {index + 1}")

    def _step_sound(self, preset: dict, k: int) -> np.ndarray:
        """Where the button landed: the step's name, if it's been made (and the DJ's on), else its pips."""
        if self.names is not None and getattr(self.station, "_has_voice", False) and getattr(self.station, "dj_on", True):
            try:
                name = self.names.clip(spoken(preset, self.label(preset)))
            except Exception:
                log.exception("button names")
                name = None
            if name is not None and len(name):
                return name
        return step_pips(k + 1)

    def _play(self, preset: dict, what: str) -> None:
        """Play a preset (tuning to it unless it's what's on already), or do it if it's an action."""
        if preset["kind"] in INSTANT:
            self._instant(preset)
            return
        if not same(preset, self.current()):
            try:
                self.apply(preset)
            except ValueError as e:
                log.warning("%s: %s", what, e)
                self._say(f"Sorry, I can't play {self.label(preset)}.")
                return
        if self.control is not None:
            self.control.play()

    def _choose(self, index: int) -> None:
        """The selector landed on a position: play it (its first step: a switch can't
        be pressed again). An empty position just beeps."""
        chain = steps(self.presets[index])
        log.info("selector %d: %s", index + 1, self.label(chain[0] if chain else None))
        if not chain:
            self._clip(beep((440.0, 330.0)), "Button")
            return
        self._play(chain[0], f"selector {index + 1}")

    def hold(self, index: int) -> None:
        """A long press: just a press (a button is set from the page, so one held
        too long can't wipe its steps). In the service menu, it answers the menu."""
        if self.menu is not None and self.menu.active:
            self.menu.press(index)
            return
        if self.selector:
            return                           # (a switch has no hold)
        self.press(index)

    def apply(self, preset: dict) -> None:
        """Play a (non-action) preset. ValueError if it can't be played here."""
        if preset["kind"] == "show":
            # the choice first, so the show starts with it (an old artist button: its theme)
            self.station.set_profile(preset["profile"] or (self.station.theme_for_artist(preset["artist"])
                                                           if preset.get("artist") else None))
            if self.station.source is not None:
                self.station.tune(None)
            if self.config_file is not None:
                save_setting(self.config_file, "broadcast_artist", None)
                save_setting(self.config_file, "broadcast_profile", self.station.profile)
        elif preset["kind"] in ("radio", "album", "book"):
            self.station.tune(preset)
        elif preset["kind"] == "playlist":
            self.station.play_playlist(preset["name"], preset["shuffle"])
        elif preset["kind"] == "programme":
            self.station.scheduler.play(preset["name"])
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
                          else "Sorry, I don't know the time yet.", always=True)
            self._clip(beep(), "Button")          # (at once: the voice may need a moment to wake)
            threading.Thread(target=tell, name="button-time", daemon=True).start()
        elif action == "news":
            self._clip(beep(), "Button")
            threading.Thread(target=self._news, name="news-now", daemon=True).start()
        elif action == "sleep" and self.control is not None:
            on = not self.control.status().get("sleep_min")
            self.control.set_sleep(SLEEP_MIN if on else 0)
            self._sleep_set(on)
        elif action == "pips":
            self._clip(pips(), "The pips")
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

    def instant(self, preset) -> dict:
        """Do an action, or play a jingle / say a message or birthday, now -- as a
        button holding it would (the desktop's double-click or drop on the radio).
        ValueError if it isn't one of those."""
        p = validate(preset)
        if p is None or p["kind"] not in INSTANT:
            raise ValueError(f"only these happen at once: {', '.join(INSTANT)}")
        self._instant(p)
        return p

    def _instant(self, preset: dict) -> None:
        """An action, or words or a jingle over whatever's on (asked for: said even with the DJ off)."""
        kind = preset["kind"]
        if kind == "action":
            self._action(preset["action"])
        elif kind == "message":
            from datetime import date
            messages = getattr(self.station, "messages", None)
            self._say(messages.words(preset["text"], date.today()) if messages is not None else preset["text"], always=True)
        elif kind == "birthday":
            from datetime import date
            from sleepradiopi.broadcast import birthdays
            age = date.today().year - preset["year"] if preset.get("year") else None
            self._say(birthdays.wish_text([{"name": preset["name"], "age": age if age and 1 <= age <= 120 else None}],
                                          self.station.builder.station), always=True)
        elif kind == "jingle":
            if self.jingle is not None:
                self.jingle(preset["path"])
            else:
                self._clip(beep((440.0, 330.0)), "Button")

    def scheduled(self, action: str) -> None:
        """An action at a programme's moment: set things on or off (a button's
        switch could go either way), else as the button does it."""
        if action == "sleep" and self.control is not None:
            if not self.control.status().get("sleep_min"):       # (already counting down: leave it be)
                self.control.set_sleep(SLEEP_MIN)
                self._sleep_set(True)
        elif action in ("noise_on", "noise_off") and self.control is not None:
            self.control.set_noise(on=action == "noise_on")
        elif action in ("dj_on", "dj_off"):
            on = action == "dj_on"
            if self.station.dj_on != on:
                self._action("dj")
        else:
            self._action(action)

    def _news(self) -> None:
        try:
            audio = self.station.news_now()
        except Exception:
            log.exception("news now failed")
            audio = None
        if audio is None:
            self._say("Sorry, there's no news to read. Is the radio online?", always=True)
        else:
            self._clip(audio, "News")

    def _clip(self, audio: np.ndarray, label_: str) -> None:
        if self.control is not None:
            self.control.play_clip(Clip(audio, "button", label_))

    def _sleep_set(self, on: bool) -> None:
        """The sleep timer never talks (it's for nodding off): just one of the pips, on or off."""
        log.info("button: sleep timer %s", f"{SLEEP_MIN} minutes" if on else "off")
        self._clip(pip(), "Button")

    def _say(self, text: str, beep_first: bool = True, always: bool = False) -> None:
        """A beep at once, then the line in the DJ's voice (made in the background:
        slow on a Zero). Just the beep without a voice -- or with the DJ switched
        off, unless the words are what was asked for (always: say the time)."""
        log.info("button: %s", text)
        if beep_first:
            self._clip(beep(), "Button")
        if not getattr(self.station, "_has_voice", False) or self.control is None:
            return
        if not always and not getattr(self.station, "dj_on", True):
            return

        def run():
            try:
                self._clip(self.station.render_speech(text), "Button")
            except Exception:
                log.exception("button: couldn't say it")
        threading.Thread(target=run, name="button-say", daemon=True).start()
