"""Save the radio's settings to a file, and load them back.

The web page's Save settings button downloads one JSON file: the settings
from config.json plus the speaker's volume. Load settings sends such a file
back -- to restore a radio after its card is re-flashed, or to copy the
settings to a second radio.

The radio keeps such files itself too (Store: the desktop's Backups folder),
in a "backups" folder beside config.json: saved with a name, loaded back,
deleted, or taken to the PC. Before one is loaded, the settings as they are
go into "Before the last load", so a load can be undone.

A radio's own set-up (where its music and voices are, its web port, its sound
card and pins) isn't in the file, and loading never changes it: it belongs
to that radio, and a wrong value could stop the station starting. Everything
in a loaded file is checked before any of it is written.
"""

from __future__ import annotations

import json
import re
import time
import types
import typing
from dataclasses import asdict, fields
from pathlib import Path

from sleepradiopi.audio.eq import BANDS, MAX_DB, validate_sounds
from sleepradiopi.broadcast import birthdays, messages, profiles
from sleepradiopi.broadcast import playlists as playlists_mod
from sleepradiopi.broadcast import programmes as programmes_mod
from sleepradiopi.broadcast.models import Chattiness
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.settings import CONFIG_LOCK, Settings, load, read_config
from sleepradiopi.playback import radio
from sleepradiopi.io import presets
from sleepradiopi.playback import podcasts as podcasts_mod

FORMAT = "sleepradiopi-settings"
VERSION = 1

# This radio's own set-up: never saved to the file or loaded from one.
LOCAL = {"hardware", "station_name", "music_folder", "jingles_folder", "audiobooks_folder", "voices_folder", "hooks_file", "http_port",
         "speaker_enabled", "speaker_device", "gpio_pin_mapping", "lcd_panel_type",
         "web_password", "update_source", "stream_source"}   # (what's on, not a setting)           # (not a Settings field: kept out of the file on purpose)
# Applied while the station runs; any other change needs a restart.
LIVE = {"speaker_mono", "speaker_eq", "speaker_highpass_hz", "broadcast_artist", "birthdays",
        "profiles", "broadcast_profile", "broadcast_chattiness", "broadcast_dj_hooks",
        "broadcast_jingle_enabled", "broadcast_jingle_every", "news_enabled", "startup_sound", "web_stream",
        "broadcast_announcer_speed", "news_speed", "radio_stations", "buttons", "noise_on", "noise_kind", "noise_mix",
        "podcasts", "messages", "buttons_night", "buttons_bank", "buttons_auto", "broadcast_dj",
        "glow_day", "glow_night", "meter_trim_db", "playlists", "knob_mode", "programmes", "programme_mode",
        "knob_long", "power_on", "speaker_sounds", "ondemand_keep"}

CHOICES = {
    "buttons_bank": {"day", "night"},
    "hardware": {"box", "cathedral"},
    "broadcast_voice": {None, "stock", "personal"},
    "news_voice": {"same", "stock", "personal"},
    "broadcast_chattiness": {c.ident for c in Chattiness},
    "noise_kind": {"white", "pink", "brown", "deep", "blue", "violet", "ambient"},
    "knob_mode": {"auto", "fine", "normal", "coarse"},
}
RANGES = {
    "broadcast_jingle_every": (1, 50),
    "broadcast_announcer_volume": (0, 4),
    "broadcast_announcer_speed": (0.5, 1.5),     # station.SPEED_MIN/MAX
    "news_speed": (0.5, 1.5),
    "news_quiet_start_min": (0, 24 * 60 - 1),
    "news_quiet_end_min": (0, 24 * 60 - 1),
    "listener_grace_s": (0, 3600),
    "speaker_volume": (0, 100),
    "knob_step": (1, 20),
    "speaker_highpass_hz": (0, 300),
    "noise_mix": (0, 100),
}
SHARED = [f for f in fields(Settings) if f.name not in LOCAL]


class BadSettings(ValueError):
    """The file isn't a settings file, or a value in it is wrong."""


def export(config_file: Path, volume: int | None) -> dict:
    """The settings file's contents (the defaults for anything not set)."""
    settings = asdict(load(config_file))
    return {
        "format": FORMAT,
        "version": VERSION,
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "settings": {f.name: settings[f.name] for f in SHARED},
        "volume": volume,
    }


def _types(name: str) -> tuple:
    t = next(f.type for f in fields(Settings) if f.name == name)
    if isinstance(t, types.UnionType) or typing.get_origin(t) is typing.Union:
        return typing.get_args(t)
    return (t,)


def _check(name: str, value):
    allowed = _types(name)
    if isinstance(value, bool):
        ok = bool in allowed
    elif isinstance(value, (int, float)):
        ok = float in allowed or (int in allowed and isinstance(value, int))
        if ok and not (value == value and abs(value) != float("inf")):
            ok = False                                  # NaN / infinity
    else:
        ok = type(value) in allowed
    if not ok:
        raise BadSettings(f"{name}: {value!r} is the wrong kind of value")
    if name in CHOICES and value not in CHOICES[name]:
        raise BadSettings(f"{name}: {value!r} isn't one of the choices")
    if name in RANGES:
        lo, hi = RANGES[name]
        if not lo <= value <= hi:
            raise BadSettings(f"{name}: {value!r} is out of range ({lo} to {hi})")
    if name == "profiles":
        try:
            return profiles.validate(value)
        except ValueError as e:
            raise BadSettings(f"profiles: {e}") from None
    if name == "programmes":
        try:
            return programmes_mod.validate(value)
        except ValueError as e:
            raise BadSettings(f"programmes: {e}") from None
    if name == "playlists":
        try:
            return playlists_mod.validate(value)
        except ValueError as e:
            raise BadSettings(f"playlists: {e}") from None
    if name == "podcasts":
        try:
            return podcasts_mod.validate_shows(value)
        except ValueError as e:
            raise BadSettings(f"podcasts: {e}") from None
    if name in ("buttons", "buttons_night"):
        try:
            return presets.validate_all(value)
        except ValueError as e:
            raise BadSettings(f"{name}: {e}") from None
    if name in presets.SLOTS:
        try:
            return presets.validate_slot(name, value)
        except ValueError as e:
            raise BadSettings(f"{name}: {e}") from None
    if name == "buttons_auto":
        try:
            return presets.validate_auto(value)
        except ValueError as e:
            raise BadSettings(f"buttons_auto: {e}") from None
    if name == "radio_stations" and value is not None:
        try:
            return radio.validate_stations(value)
        except ValueError as e:
            raise BadSettings(f"radio_stations: {e}") from None
    if name == "birthdays":
        try:
            return birthdays.validate(value)
        except ValueError as e:
            raise BadSettings(f"birthdays: {e}") from None
    if name == "messages":
        try:
            return messages.validate(value)
        except ValueError as e:
            raise BadSettings(f"messages: {e}") from None
    if name == "ondemand_keep":
        if not isinstance(value, list) or len(value) > 500 or not all(
                isinstance(p, str) and p and len(p) <= 1000 and ".." not in p.split("/") for p in value):
            raise BadSettings("ondemand_keep: a list of paths under On demand")
        return value
    if name == "speaker_sounds":
        try:
            return validate_sounds(value)
        except ValueError as e:
            raise BadSettings(f"speaker_sounds: {e}") from None
    if name == "speaker_eq":
        if not all(k in BANDS and isinstance(v, (int, float)) and not isinstance(v, bool)
                   and -MAX_DB <= v <= MAX_DB for k, v in value.items()):
            raise BadSettings(f"speaker_eq: {value!r} isn't bass/mid/treble in dB")
    return value


def parse(data) -> tuple[dict, int | None]:
    """Check a loaded file. Returns (settings to apply, volume or None).
    Keys this version doesn't know, and this radio's own set-up, are skipped."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BadSettings("this isn't a Sleep Radio settings file")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise BadSettings("this settings file is from a newer version of the radio")
    raw = data.get("settings")
    if not isinstance(raw, dict):
        raise BadSettings("the file has no settings in it")
    shared = {f.name for f in SHARED}
    settings = {k: _check(k, v) for k, v in raw.items() if k in shared}
    volume = data.get("volume")
    if volume is not None:
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
            raise BadSettings(f"volume: {volume!r} is out of range (0 to 100)")
    return settings, volume


def apply(config_file: Path, settings: dict) -> set[str]:
    """Write the settings into config_file, keeping everything else in it.
    Returns the names of the settings that changed."""
    with CONFIG_LOCK:
        conf = read_config(config_file)          # (unreadable: ValueError, nothing written)
        before = asdict(load(config_file)) if config_file.exists() else asdict(Settings())
        changed = {k for k, v in settings.items() if before.get(k) != v}
        conf.update(settings)
        Settings(**{k: v for k, v in conf.items() if k in {f.name for f in fields(Settings)}})
        write_atomic(config_file, json.dumps(conf, indent=2) + "\n")
    return changed


MAX_KEPT = 30                       # saved settings the radio keeps
BEFORE = "Before the last load"     # the settings as they were, kept when one is loaded


class Store:
    """Saved settings kept on the radio: one <name>.json each, in a folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    @staticmethod
    def name(name) -> str:
        """A name that's safe as a file's (BadSettings if not)."""
        if not isinstance(name, str):
            raise BadSettings("its name must be text")
        name = " ".join(name.split())
        if not name or len(name) > 60 or name.startswith(".") or not re.fullmatch(r"[\w .,()'&+-]+", name):
            raise BadSettings("a name is up to 60 letters, numbers, spaces and . , ( ) ' & + -")
        return name

    def _path(self, name) -> Path:
        return self.folder / (self.name(name) + ".json")

    def list(self) -> list[dict]:
        """What's kept, newest first: [{"name", "saved", "bytes"}]."""
        out = []
        for p in self.folder.glob("*.json") if self.folder.is_dir() else ():
            try:
                st = p.stat()
                out.append({"name": p.stem, "saved": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                            "bytes": st.st_size, "_t": st.st_mtime})
            except OSError:
                continue
        out.sort(key=lambda b: b.pop("_t"), reverse=True)
        return out

    def save(self, data: dict, name=None, replace: bool = False) -> str:
        """Keep these settings (an export()) under a name; without one, the date
        and time. A name that's taken gets a number, unless replace. Returns the name."""
        parse(data)                                   # (only ever a file that could be loaded back)
        name = self.name(name if name else time.strftime("%Y-%m-%d %H.%M"))
        taken = {b["name"].lower() for b in self.list()}
        if not replace:
            base, n = name, 2
            while name.lower() in taken:
                name, n = f"{base} ({n})", n + 1
        if name.lower() not in taken and len(taken) >= MAX_KEPT:
            raise BadSettings(f"the radio keeps up to {MAX_KEPT}: delete one first")
        self.folder.mkdir(parents=True, exist_ok=True)
        write_atomic(self._path(name), json.dumps(data, indent=2) + "\n")
        return name

    def read(self, name) -> dict:
        try:
            return json.loads(self._path(name).read_text())
        except (OSError, ValueError):
            raise BadSettings(f"there's no saved settings file called {name!r}") from None

    def delete(self, name) -> None:
        try:
            self._path(name).unlink()
        except OSError:
            raise BadSettings(f"there's no saved settings file called {name!r}") from None
