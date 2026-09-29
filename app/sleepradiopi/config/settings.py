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
    broadcast_profile: str | None = None  # the profile playing (instead of an artist); None = none
    broadcast_chattiness: str = "maximum"
    radio_stations: list | None = None    # internet radio: [{"name", "url", "info"?}]; None = the starter set
    buttons: list = field(default_factory=list)   # the 4 preset buttons (io/presets.py); null = empty
    stream_source: dict | None = None     # a station or album playing instead of the show; None = the show
    birthdays: list = field(default_factory=list)  # [{"name", "day", "month", "year"?}]: the DJ wishes them on the day
    broadcast_jingle_enabled: bool = True
    broadcast_jingle_every: int = 4
    broadcast_announcer_volume: float = 0.4    # x the speech level; 0.4 ~ level with the music
    broadcast_announcer_speed: float = 0.85
    broadcast_dj_hooks: bool = True
    news_enabled: bool = True
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
    speaker_highpass_hz: int = 0      # low cut for the speaker (~140 with the box's bass port); 0 = off
    knob_step: int = 2                # volume change per click of the knob
    gpio_pin_mapping: dict = field(default_factory=dict)
    lcd_panel_type: str | None = None


def load(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save_setting(path: Path, key: str, value) -> None:
    """Set one key in the config file, keeping everything else in it as it is."""
    try:
        conf = json.loads(path.read_text())
    except (OSError, ValueError):
        conf = {}
    conf[key] = value
    write_atomic(path, json.dumps(conf, indent=2) + "\n")


def save(path: Path, settings: Settings) -> None:
    write_atomic(path, json.dumps(asdict(settings), indent=2))
