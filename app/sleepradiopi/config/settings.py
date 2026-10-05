"""Flat-file settings store (JSON on disk).

Covers what SleepRadio's SettingsRepository persists via DataStore, adapted
for plain filesystem paths instead of SAF tree URIs. Broadcast defaults
mirror the Android app as the user runs it (from its 2026-09-21 backup):
maximum chattiness, jingles every 4 tracks, 70s hooks on, news on with
quiet hours 23:00-06:00, DJ at 0.85x and the newsreader at 0.70x.

Unknown keys in the file are ignored, so an older/newer config never stops
the station from starting.
"""

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic

DEFAULT_PATH = Path.home() / ".config" / "sleepradiopi" / "config.json"


@dataclass
class Settings:
    music_folder: str | None = None       # default ~/media/music
    jingles_folder: str | None = None     # default ~/media/jingles
    audiobooks_folder: str | None = None  # default: "audiobooks" next to the music folder
    voices_folder: str | None = None      # default <repo>/voices
    hooks_file: str | None = None         # default: the bundled sleepradiopi/data/dj_hooks_70s.txt
    broadcast_voice: str | None = "stock"      # "stock" or "personal"; None = music and jingles only
    broadcast_artist: str | None = None   # artist radio ("The Beatles" -> "Beatles Radio"); None = everything
    profiles: list = field(default_factory=list)   # [{"name": "Friday List", "artists": [...]}]
    playlists: list = field(default_factory=list)  # [{"name": "Sunday", "tracks": [["music", "A/B/01 - x.mp3"], ...]}]
    programmes: list = field(default_factory=list) # running orders of blocks: see broadcast/programmes.py
    broadcast_profile: str | None = None  # the profile playing (instead of an artist); None = none
    broadcast_chattiness: str = "maximum"
    podcasts: list = field(default_factory=list)  # the shows followed: [{"feed_url", "title", "author"}]
    radio_stations: list | None = None    # internet radio: [{"name", "url", "info"?}]; None = the starter set
    callsign: str | None = None           # the owner's amateur radio callsign: what a room is joined under (playback/room.py)
    room_level: int = 100                 # how loud rooms are, % (25-150): a voice, not levelled like music (playback/room.py)
    noise_reduction: str = "off"          # receivers: the hiss under a voice turned down -- off, light, strong or rig (audio/denoise.py)
    monitor_music: int = 10               # how loud the programme stays under a monitored room, % of itself (0-100) (playback/monitor.py)
    monitor_rooms: list = field(default_factory=list)   # rooms heard over whatever's playing: [{"name", "url"}] (playback/monitor.py)
    station_name: str | None = None       # the radio's name as spoken and shown; None: by the hardware
                                          # ("Sleep Radio", or "Phonosphere" for the cathedral) -- config/brand.py
    glow_day: int = 60                    # the cathedral's grille light, % (the day set of presets)...
    glow_night: int = 15                  # ...and the night set
    meter_trim_db: float = 0.0            # the VU needle: + reads higher
    hardware: str = "box"                 # "box": 4 preset buttons; "cathedral": the Phonosphere -- a 6-way
                                          # rotary selector, a back button, a VU needle and a grille light
    buttons: list = field(default_factory=list)   # the preset buttons (io/presets.py); null = empty (the day set)
    buttons_night: list = field(default_factory=list)   # ...the night set
    buttons_bank: str = "day"             # the set in use: "day" or "night"
    buttons_auto: dict = field(default_factory=dict)    # {"on", "night_min", "day_min"}: swap sets by the clock
    knob_long: dict | None = None         # what holding the knob for 3 s does (a preset, as on a button); None: say the address
    power_on: dict | None = None          # what plays when the radio's switched on (a preset); None: what was on last
    broadcast_dj: bool = True             # False: the show is music only (no welcome, links, time checks)
    stream_source: dict | None = None     # a station or album playing instead of the show; None = the show
    birthdays: list = field(default_factory=list)  # [{"name", "day", "month", "year"?}]: the DJ wishes them on the day
    messages: dict = field(default_factory=dict)   # notes the DJ reads through the day (broadcast/messages.py)
    broadcast_jingle_enabled: bool = True
    broadcast_jingle_every: int = 4
    broadcast_announcer_volume: float = 0.4    # x the speech level; 0.4 ~ level with the music
    broadcast_announcer_speed: float = 0.85
    broadcast_dj_hooks: bool = True
    news_enabled: bool = True
    broadcast_time_checks: bool = True    # the DJ says the time in some links (a theme may set its own)
    news_voice: str = "same"          # "same" = the DJ voice (one voice fits a Pi Zero 2 W's RAM)
    news_speed: float = 0.70
    news_quiet_hours: bool = True
    news_quiet_start_min: int = 23 * 60
    news_quiet_end_min: int = 6 * 60
    http_port: int = 80
    listener_grace_s: float = 30.0   # keep broadcasting this long after the last listener leaves
    # Play through the Pi's own sound card (the MiniAmp) from start-up, with a
    # rotary encoder for volume and its push switch for pause. Off by default
    # so a desktop test run doesn't start playing out loud.
    update_source: str = "github:dylan7474/SleepRadioPi-OS"   # where updates come from (or a manifest URL)
    web_stream: bool = False          # listen in a browser too (always on without a speaker)
    startup_sound: bool = True        # a chime (and "warming up") at power-on, while the station loads
    speaker_enabled: bool = False
    speaker_device: str = "default"   # ALSA device for aplay
    speaker_volume: int = 30          # 0-100 on first start; after that the knob's last setting
    speaker_mono: bool = False        # mix left and right so both speakers play the same
    speaker_eq: dict = field(default_factory=dict)  # {"bass", "mid", "treble"}: dB, -12..12
    noise_on: bool = False            # the noise layer (audio/noise.py), on top of whatever plays
    noise_kind: str = "pink"          # white, pink, brown, deep, blue, violet, ambient
    noise_mix: int = 50               # balance: 0 programme only, 50 both full, 100 noise only
    ondemand_keep: list = field(default_factory=list)    # On demand files and folders that remember their place, like a book
    speaker_sounds: list = field(default_factory=list)   # saved speaker set-ups: [{"name", "eq", "highpass", "mono"}]
    speaker_highpass_hz: int = 0      # low cut for the speaker (~140 with the box's bass port); 0 = off
    knob_step: int = 2                # volume change per click of the knob ("normal")
    programme_mode: bool = False      # silent unless a programme is on (an alarm clock): see broadcast/programmes.py
    knob_mode: str = "auto"           # auto (slow = fine, fast = big steps), fine (1), normal (knob_step), coarse (5)
    gpio_pin_mapping: dict = field(default_factory=dict)
    lcd_panel_type: str | None = None


def load(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


log = logging.getLogger(__name__)
CONFIG_LOCK = threading.RLock()   # one read-change-write of the config file at a time


def read_config(path: Path) -> dict:
    """The config file as a dict ({} if there isn't one). If it's there but can't
    be read, ValueError -- and a copy is kept aside -- never {}: writing {} plus
    one key back lost every other setting once (2026-09-30)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        conf = json.loads(text)
        if not isinstance(conf, dict):
            raise ValueError("not a JSON object")
        return conf
    except ValueError as e:
        bad = path.with_name(f"{path.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            bad.write_text(text)
        except OSError:
            pass
        raise ValueError(f"the settings file couldn't be read ({e}); nothing was saved, and a copy is kept as {bad.name}") from e


def save_setting(path: Path, key: str, value) -> None:
    """Set one key in the config file, keeping everything else in it as it is.
    If the file can't be read, nothing is written (logged)."""
    with CONFIG_LOCK:
        try:
            conf = read_config(path)
        except (OSError, ValueError) as e:
            log.error("setting %s not saved: %s", key, e)
            return
        conf[key] = value
        write_atomic(path, json.dumps(conf, indent=2) + "\n")


def save(path: Path, settings: Settings) -> None:
    write_atomic(path, json.dumps(asdict(settings), indent=2))
