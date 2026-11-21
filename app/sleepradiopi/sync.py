"""Radios that know each other: find the others on the network, pair, and keep
some of their things the same both ways (two-way sync).

Each radio has an id of its own (radio_id, made once). It says it's there on
the network now and then (a small UDP broadcast, BEACON_PORT: the stdlib only,
no daemon): the desktop's Other radios lists the ones heard, and one can be
added by its address too. Pairing is both ways: each keeps the other in
sync_peers (by id -- an address can change; a beacon keeps it current).

Sync is pull-based and clock-free. A radio asks a paired one for its state
(GET /api/sync/state) and merges it with its own, three ways: against the
state the two last agreed on (the base, kept in sync-base.json beside the
config). It sends the result back (POST /api/sync/adopt: taken up only if
that radio hasn't changed meanwhile), takes it up itself, and both keep it
as the new base. An item only the other side changed is taken; one only this side
changed is kept; added on either, it's added; deleted on one and untouched on
the other, it goes. The same item changed differently on both: a change beats
a delete, otherwise a fixed rule every radio applies alike (the larger, as
text) -- so both end up the same with no clocks (a radio with no RTC can't be
trusted for "the latest").

Guards: with no base (the first sync with a radio, or after a card is
re-flashed) the merge only adds -- never deletes. A merge that would delete
much of a collection is held and shown on the page instead (Sync anyway lets
it through once). A settings backup is saved before the first sync with a
radio. A radio with a web password doesn't take part (it says so).

What's synced is COLLECTIONS; each radio's own set-up, buttons, volume and
what's playing never are. Writes happen only when something actually changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.settings import CONFIG_LOCK, read_config, save_setting
from dataclasses import asdict

from sleepradiopi.broadcast import birthdays as birthdays_mod
from sleepradiopi.broadcast import messages as messages_mod
from sleepradiopi.broadcast import playlists as playlists_mod
from sleepradiopi.broadcast import profiles as profiles_mod
from sleepradiopi.broadcast import programmes as programmes_mod
from sleepradiopi.broadcast import selector as selector_mod
from sleepradiopi.config import backup as backup_mod
from sleepradiopi.config.settings import Settings
from sleepradiopi import media as media_mod
from sleepradiopi import voices as voices_mod
from sleepradiopi import wifi as wifi_mod
from sleepradiopi.playback import podcasts as podcasts_mod
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

PROTOCOL = 3                    # what a sync request looks like (not the app's version: radios mid-update still sync)
BEACON_PORT = 47474
BEACON_EVERY_S = 60
SEEN_FOR_S = 3 * BEACON_EVERY_S + 30     # a radio not heard for this long is no longer listed as on the network
SYNC_EVERY_S = 300
TIMEOUT_S = 6
SLOW_TIMEOUT_S = 30                 # (a radio's state, with its libraries, and taking up a merge: seconds on a Zero)
HOLD_DELETES = 0.3              # a merge deleting more than this share of a collection (and more than HOLD_MIN) waits
HOLD_MIN = 3
BASE_FILE = "sync-base.json"


# --- The collections synced: how each is read, written, keyed and checked ---------------------------------------

def _stations_in(conf: dict) -> list[dict]:
    saved = conf.get("radio_stations")
    if saved is None:                            # (never saved: the starter set, as the page shows it)
        return [dict(s) for s in radio_mod.DEFAULT_STATIONS]
    try:
        return radio_mod.validate_stations(saved)
    except ValueError:
        return []


def _names_in(conf: dict) -> dict:
    names = conf.get("receiver_names")
    return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}


def _check_names(value) -> dict:
    if not isinstance(value, dict) or len(value) > 200:
        raise ValueError("receiver_names: {address: name}")
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k.startswith(("http://", "https://", "ysf://", "fcs://")) \
                or len(k) > 300 or len(v) > 60:
            raise ValueError("receiver_names: {address: name}")
        out[k] = v
    return out


def _plain(read_key: str, check, default=list):
    """A collection that's one setting of the same name: (read, check, write)."""
    def read(conf):
        v = conf.get(read_key)
        try:
            return check(v if v is not None else default())
        except (ValueError, TypeError):
            return default()
    return read, check, lambda conf, value: {read_key: value}


# Programmes: copied both ways, but whether one starts by itself is each radio's own (one that arrives
# from another radio arrives switched off) -- else an alarm set on one would sound on every paired radio.
def _programmes_in(conf: dict) -> list[dict]:
    try:
        return [{k: v for k, v in p.items() if k != "auto"} for p in programmes_mod.validate(conf.get("programmes") or [])]
    except (ValueError, TypeError):
        return []


def _check_programmes(value) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "auto"} for p in programmes_mod.validate(
        [{**p, "auto": False} if isinstance(p, dict) else p for p in (value or [])])]


def _programmes_out(conf: dict, value) -> dict:
    auto = {p["name"].lower(): bool(p.get("auto")) for p in (conf.get("programmes") or []) if isinstance(p, dict) and "name" in p}
    return {"programmes": programmes_mod.validate([{**p, "auto": auto.get(p["name"].lower(), False)} for p in value])}


# Messages: the list (each message keyed by its words, times and theme: an edit is a delete and an add),
# and the times they're read at, kept apart (a dict of their own).
def _messages_cfg(conf: dict) -> dict:
    try:
        return messages_mod.validate(conf.get("messages") or {})
    except (ValueError, TypeError):
        return messages_mod.validate({})


def _messages_in(conf: dict) -> list[dict]:
    return _messages_cfg(conf)["list"]


def _check_messages(value) -> list[dict]:
    return messages_mod.validate({"list": value})["list"]


def _messages_out(conf: dict, value) -> dict:
    return {"messages": messages_mod.validate({**_messages_cfg(conf), "list": value})}


def _times_in(conf: dict) -> dict:
    return {k: v for k, v in _messages_cfg(conf).items() if k != "list"}


def _check_times(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("message times: {...}")
    return {k: v for k, v in messages_mod.validate({**value, "list": []}).items() if k != "list"}


def _times_out(conf: dict, value) -> dict:
    return {"messages": messages_mod.validate({**_messages_cfg(conf), **value})}


# The radio's other settings, each kept the same on both -- all but its own: what its hardware is and how
# it's set up (speakers, amp, buttons, knob, glow, meter), what it's playing, its volume, and the jobs one
# radio does for a receiver (reporting, surveys, calibrating), which two radios mustn't both do.
VOICE_SETTINGS = {"ebook_voice"}     # (a synced setting naming a voice: not taken up live till that voice is here)
RADIO_OWN = {"speaker_mono", "speaker_eq", "speaker_highpass_hz", "speaker_volume", "speaker_sounds", "eq_slots",
             "eq_by_type", "eq_auto", "meter_trim_db", "glow_day", "glow_night", "knob_mode", "knob_step", "knob_long",
             "buttons", "buttons_night", "buttons_bank", "buttons_auto", "power_on", "programme_mode",
             "broadcast_profile", "broadcast_artist", "noise_on", "noise_kind", "noise_mix", "calibrate_itself",
             "survey_itself", "report_heard", "listener_grace_s", "monitor_music", "monitor_rooms", "web_stream",
             "ondemand_keep",
             "broadcast_voice", "news_voice",
             # (the room's and the speaker's, not the programme's -- each radio's own since 1.9.96)
             "news_quiet_hours", "news_quiet_start_min", "news_quiet_end_min", "room_level", "noise_reduction",
             "broadcast_announcer_volume", "stream_evenness", "startup_sound", "notify", "lightning_bell",
             # (every notification setting is each radio's own, since 1.9.105: the bell, what's shown, the old Alerts switch)
             "lightning_when", "aircraft_off", "conditions_alerts",
             "background_url"}    # (the DJ's and the news's voice: each radio's choice; the packs themselves sync)
COLLECTION_KEYS = {"radio_stations", "receiver_names", "profiles", "playlists", "programmes", "messages",
                   "birthdays", "podcasts", "dj_hooks_own", "dj_hooks_changes"}


def _synced_settings() -> list[str]:
    return sorted(f.name for f in backup_mod.SHARED if f.name not in RADIO_OWN | COLLECTION_KEYS)


# The settings that are shared, in groups each shared (or not) on its own (since 1.9.97). A shared setting no group
# names yet goes with the DJ's (a test makes sure there are none).
SETTING_GROUPS = {
    "dj": ("broadcast_chattiness", "broadcast_dj", "broadcast_dj_hooks", "broadcast_jingle_enabled",
           "broadcast_jingle_every", "broadcast_time_checks", "broadcast_announcer_speed", "news_enabled", "news_speed"),
    "ebook": ("ebook_voice", "ebook_pause", "ebook_speed", "ebook_pace"),
    "identity": ("callsign", "locator"),
    "outside": ("conditions_every_h", "lightning_watch", "aircraft_url",
                "aircraft_range", "aircraft_watch", "warnings_region"),
}


def _group_of(key: str) -> str:
    return next((g for g, keys in SETTING_GROUPS.items() if key in keys), "dj")


def _settings_of(group: str) -> list[str]:
    return [k for k in _synced_settings() if _group_of(k) == group]


def _settings_reader(group: str):
    def read(conf: dict) -> dict:
        defaults = asdict(Settings())
        out = {}
        for k in _settings_of(group):
            try:
                out[k] = backup_mod._check(k, conf.get(k, defaults.get(k)))
            except ValueError:
                out[k] = defaults.get(k)
        return out
    return read


def _settings_checker(group: str):
    def check(value) -> dict:
        if not isinstance(value, dict):
            raise ValueError("settings: {...}")
        known = set(_settings_of(group))
        return {k: backup_mod._check(k, v) for k, v in value.items() if k in known}     # (one this version hasn't: skipped)
    return check


COLLECTIONS = {
    # name: (read from the config, check what another radio sent, the item's key -- None: a dict, keyed
    # already --, the config keys it's written as)
    "radio_stations": (_stations_in, radio_mod.validate_stations, lambda s: s["url"], lambda conf, v: {"radio_stations": v}),
    "receiver_names": (_names_in, _check_names, None, lambda conf, v: {"receiver_names": v}),
    "profiles": (*_plain("profiles", profiles_mod.validate)[:2], lambda p: p["name"].lower(), _plain("profiles", profiles_mod.validate)[2]),
    "playlists": (*_plain("playlists", playlists_mod.validate)[:2], lambda p: p["name"].lower(), _plain("playlists", playlists_mod.validate)[2]),
    "programmes": (_programmes_in, _check_programmes, lambda p: p["name"].lower(), _programmes_out),
    "messages": (_messages_in, _check_messages, lambda m: _canon(m), _messages_out),
    "message_times": (_times_in, _check_times, None, _times_out),
    "birthdays": (*_plain("birthdays", birthdays_mod.validate)[:2], lambda b: f"{b['name'].lower()}|{b['day']}|{b['month']}", _plain("birthdays", birthdays_mod.validate)[2]),
    "podcasts": (*_plain("podcasts", podcasts_mod.validate_shows)[:2], lambda p: p["feed_url"], _plain("podcasts", podcasts_mod.validate_shows)[2]),
    "dj_hooks_own": (*_plain("dj_hooks_own", selector_mod.validate_own_hooks)[:2], lambda line: line, _plain("dj_hooks_own", selector_mod.validate_own_hooks)[2]),
    # (changes to the 70s lines, by the line as it comes: a new wording or switched off; Restore deletes the change)
    "dj_hooks_changes": (*_plain("dj_hooks_changes", selector_mod.validate_hook_changes, dict)[:2], None,
                         _plain("dj_hooks_changes", selector_mod.validate_hook_changes, dict)[2]),
    **{f"settings:{g}": (_settings_reader(g), _settings_checker(g), None, lambda conf, v: dict(v)) for g in SETTING_GROUPS},
}
# Files kept the same too: each kind a collection of {path: size}, read from the card, not the config. A file
# one radio lacks is fetched from the other (GET /api/media/download); one deleted on either goes from both.
FILE_KINDS = ("jingles",)
MAX_SYNC_FILES = 5000


def _size_of(value) -> int:
    """A file's size from its entry: "size:checksum" (or a bare size)."""
    return value if isinstance(value, int) else int(str(value).split(":", 1)[0])


def _check_files(value) -> dict:
    if not isinstance(value, dict) or len(value) > MAX_SYNC_FILES:
        raise ValueError("files: {path: size:checksum}")
    out = {}
    for path, entry in value.items():
        parts = path.split("/") if isinstance(path, str) else []
        ok_entry = (isinstance(entry, int) and not isinstance(entry, bool)) or \
            (isinstance(entry, str) and re.fullmatch(r"\d{1,12}:[0-9a-f]{16}", entry) is not None)
        if not parts or len(path) > 400 or any(not p or p in (".", "..") or p.startswith(".") for p in parts) \
                or not ok_entry or not 0 <= _size_of(entry) <= media_mod.MAX_FILE:
            raise ValueError(f"files: {str(path)[:60]!r} isn't a file's path and size")
        out[path] = entry
    return out


for _kind in FILE_KINDS:
    COLLECTIONS[f"files:{_kind}"] = (None, _check_files, None, None)


# The DJ's voices: which voice packs each radio has, by name. Only ever added -- a voice one radio lacks is packed
# by the other (GET /api/sync/voice) and installed the usual way; one of the same name is never replaced (two radios'
# "personal" voices may differ) and none is deleted (voices aren't deleted from the page either).
def _check_voices(value) -> dict:
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("voices: {name: 1}")
    for name in value:
        voices_mod.check_name(name)
    return {name: 1 for name in value}


COLLECTIONS["voices"] = (None, _check_voices, None, None)


# Wi-Fi networks added on the page (wifi.json: the WPA key, never the password). Only ever added, at the end of the
# list (a radio's own order of preference stays) -- never removed, which could cut a radio off -- and only ever told
# to a paired radio, asking from its own address. The set-up hotspot and "no Wi-Fi" stay each radio's own.
def _check_wifi(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("wifi: a list of networks")
    out = []
    for n in value:
        if not isinstance(n, dict) or not isinstance(n.get("ssid"), str) or not 1 <= len(n["ssid"].encode()) <= 32:
            raise ValueError("wifi: a network's name")
        if n.get("open") is True:
            out.append({"ssid": n["ssid"], "open": True})
        elif isinstance(n.get("psk"), str) and re.fullmatch(r"[0-9a-f]{64}", n["psk"]):
            out.append({"ssid": n["ssid"], "psk": n["psk"]})
        else:
            raise ValueError(f"wifi: {n['ssid']!r} has no key")
    return out


COLLECTIONS["wifi"] = (None, _check_wifi, lambda n: n["ssid"], None)


# Places: where each audiobook, ebook, podcast episode and kept On demand thing was left (and whether an episode was
# heard to the end) -- the radio's book_positions, {key: {"pos_ms", "at", "done"?}}. Only ever added or moved on:
# where both radios have a place for the same thing, the one saved later wins (so only between radios whose clocks
# are set: one that isn't doesn't take part), and the book playing on a radio is never moved under it.
MAX_PLACES = 5000


def _check_places(value) -> dict:
    if not isinstance(value, dict) or len(value) > MAX_PLACES:
        raise ValueError("places: {key: {pos_ms, at}}")
    out = {}
    for key, p in value.items():
        if not isinstance(key, str) or not 0 < len(key) <= 1000 or not isinstance(p, dict) \
                or not isinstance(p.get("pos_ms"), int) or isinstance(p.get("pos_ms"), bool) or p["pos_ms"] < 0 \
                or not isinstance(p.get("at"), (int, float)) or isinstance(p.get("at"), bool) or p["at"] < 0:
            raise ValueError(f"places: {str(key)[:60]!r} isn't a place")
        out[key] = {"pos_ms": p["pos_ms"], "at": float(p["at"]), **({"done": True} if p.get("done") is True else {})}
    return out


def _clean_places(value) -> dict:
    """This radio's own places, the ones that check out (one odd entry never stops a sync)."""
    out = {}
    for key, p in (value or {}).items():
        try:
            out.update(_check_places({key: p}))
        except ValueError:
            continue
    return out


COLLECTIONS["places"] = (None, _check_places, None, None)


# The media: music, On demand, audiobooks, ebooks -- shared only with a paired radio both radios have ticked it
# for (sync_peers' "media", set on both at once: share_media). Each a collection of {key: "size:.ext"}: a sound
# file is keyed without its extension, so the same track as a FLAC on one radio and an MP3 on the other is one
# item, and where they differ the smaller wins (merge's _pick) -- the other radio's copy is replaced. Files are
# known by name and size alone (a whole library can't be hashed on a Zero). The copying is done by one
# background worker, a file at a time, never while the radio is playing; what the two agree on (the base) is
# kept only once every file has arrived, so a copy cut short never turns into deletes.
MEDIA_KINDS = ("music", "ondemand", "audiobooks", "ebooks")
MEDIA_WORDS = {"music": "music files", "ondemand": "On demand files", "audiobooks": "audiobook files", "ebooks": "ebook files"}
MAX_MEDIA_FILES = 60000
SOUNDS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".m4b", ".aac"}
MEDIA_WAIT_S = 30                          # (playing: looked at again this often)
TREE_MAX_AGE_S = 1800                      # (a library's file list, kept between its changes: read again at least this often)
MEDIA_HOLD_S = 7200                        # a library list that differs waits while either radio plays, but no longer than this
TOMBSTONE_DAYS = 7                         # (a delete made while a library isn't agreed yet: remembered this long at most)


def _syncable(kind: str, path: str) -> bool:
    """A file the other radio's storage would take (media.receive): what plays there, and cover pictures."""
    if any(p.startswith(".") for p in path.split("/")):
        return False
    ext = ("." + path.rsplit(".", 1)[1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    playable = media_mod.BOOK_FILES if kind == "audiobooks" else media_mod.EBOOK_FILES if kind == "ebooks" \
        else media_mod.AUDIO_EXTENSIONS
    return ext in playable or ext in media_mod.IMAGES


def _media_entries(kind: str, files: dict) -> dict:
    """A library's {path: size} as the collection {key: "size:.ext"} (see above). A radio with two copies of
    a track (a FLAC and an MP3 beside it): the smaller is the track; the other is kept as a file of its own."""
    out: dict = {}
    for path in sorted(files, key=lambda p: (files[p], p)):
        size = files[path]
        if not _syncable(kind, path) or not 0 < size <= media_mod.MAX_FILE:
            continue
        stem, dot, ext = path.rpartition(".")
        if dot and "/" not in ext and ("." + ext.lower()) in SOUNDS and stem and stem not in out:
            out[stem] = f"{size}:.{ext}"
        elif path not in out:
            out[path] = f"{size}:"
    return out


def _media_paths(entries: dict) -> dict:
    """The collection back as the files it stands for: {path: size}."""
    out = {}
    for key, value in entries.items():
        size, _, ext = str(value).partition(":")
        out[key + ext] = int(size)
    return out


def _check_media(value) -> dict:
    if not isinstance(value, dict) or len(value) > MAX_MEDIA_FILES:
        raise ValueError("media: {path: size:.ext}")
    for path, entry in value.items():
        parts = path.split("/") if isinstance(path, str) else []
        if not parts or len(path) > 400 or any(not p or p in (".", "..") or p.startswith(".") for p in parts) \
                or not isinstance(entry, str) or not re.fullmatch(r"\d{1,12}:(\.[A-Za-z0-9]{1,8})?", entry) \
                or not 0 < _size_of(entry) <= media_mod.MAX_FILE:
            raise ValueError(f"media: {str(path)[:60]!r} isn't a file's path and size")
    return dict(value)


def _check_kinds(value) -> list[str]:
    if not isinstance(value, list) or any(k not in MEDIA_KINDS for k in value):
        raise ValueError(f"kinds: a list of {', '.join(MEDIA_KINDS)}")
    return [k for k in MEDIA_KINDS if k in value]


for _kind in MEDIA_KINDS:
    COLLECTIONS[f"media:{_kind}"] = (None, _check_media, None, None)

UNION_ONLY = {"voices", "wifi", "places"}  # (only ever added: merged as a first sync is, every time)
WORDS = {**{f"files:{k}": k for k in FILE_KINDS}, **{f"media:{k}": w for k, w in MEDIA_WORDS.items()},
         "voices": "voices", "wifi": "Wi-Fi networks", "places": "places in books and podcasts", "radio_stations": "stations", "receiver_names": "receiver names", "profiles": "themes", "playlists": "playlists",
         "programmes": "programmes", "messages": "messages", "message_times": "message times", "birthdays": "birthdays",
         "podcasts": "podcasts", "dj_hooks_own": "DJ lines", "dj_hooks_changes": "changes to the 70s DJ lines",
         "settings:dj": "DJ and news settings",
         "settings:ebook": "ebook reading settings", "settings:identity": "station identity settings",
         "settings:outside": "outside settings"}


# What the Other radios window calls each thing, in the order it lists them.
LABELS = {"radio_stations": "Stations", "receiver_names": "Receiver names", "profiles": "Themes",
          "files:jingles": "Theme jingles", "dj_hooks_own": "Your DJ lines", "dj_hooks_changes": "70s lines changed",
          "playlists": "Playlists",
          "programmes": "Programmes", "messages": "Messages", "message_times": "Message times",
          "birthdays": "Birthdays", "podcasts": "Podcasts", "voices": "Voices", "wifi": "Wi-Fi networks",
          "places": "Places in books and podcasts",
          "settings:dj": "DJ and news", "settings:ebook": "Ebook reading", "settings:identity": "Station identity",
          "settings:outside": "The world outside", "media:music": "Music", "media:ondemand": "On demand",
          "media:audiobooks": "Audiobooks", "media:ebooks": "Ebooks"}


# What can be shared with a paired radio, a tick each (since 1.9.97): key -> (what the window calls it, the
# collections it covers). Set for both radios at once (sync_peers' "share"); a new pairing shares all but the media.
SHARE = {
    "stations": ("Stations and receivers", ("radio_stations", "receiver_names")),
    "themes": ("Themes, their jingles and your DJ lines", ("profiles", "files:jingles", "dj_hooks_own", "dj_hooks_changes")),
    "playlists": ("Playlists", ("playlists",)),
    "programmes": ("Programmes", ("programmes",)),
    "messages": ("Messages", ("messages", "message_times")),
    "birthdays": ("Birthdays", ("birthdays",)),
    "podcasts": ("Podcasts", ("podcasts",)),
    "voices": ("Voices", ("voices",)),
    "wifi": ("Wi-Fi networks", ("wifi",)),
    "set:dj": ("DJ and news settings", ("settings:dj",)),
    "set:ebook": ("Ebook reading", ("settings:ebook",)),
    "set:identity": ("Station identity", ("settings:identity",)),
    "set:outside": ("The world outside", ("settings:outside",)),
    "music": ("Music", ("media:music",)),
    "ondemand": ("On demand", ("media:ondemand",)),
    "audiobooks": ("Audiobooks", ("media:audiobooks",)),
    "ebooks": ("Ebooks", ("media:ebooks",)),
    "places": ("Places in books and podcasts", ("places",)),
}
DEFAULT_SHARE = [k for k in SHARE if k not in MEDIA_KINDS and k != "places"]
# What a programme's or a playlist's things need on the other radio, by share key (for the window's warnings).
NEEDS = {"playlist": "playlists", "book": "audiobooks", "podcast": "podcasts", "episode": "podcasts", "list": "themes",
         "jingle": "themes"}


def _check_share(value) -> list[str]:
    if not isinstance(value, list) or any(k not in SHARE for k in value):
        raise ValueError(f"share: a list of {', '.join(SHARE)}")
    return [k for k in SHARE if k in value]


def share_of(peer: dict | None) -> list[str]:
    """What's shared with a paired radio. One paired before 1.9.97 shares what it did then: all but the media,
    and the media it had ticked."""
    if not isinstance(peer, dict):
        return []
    if isinstance(peer.get("share"), list):
        return [k for k in SHARE if k in peer["share"]]
    media = peer.get("media") if isinstance(peer.get("media"), list) else []
    return DEFAULT_SHARE + [k for k in MEDIA_KINDS if k in media]


def _needs(conf: dict) -> dict:
    """{share key: {needed share key: how many}}: what the programmes and playlists here use."""
    out: dict = {"programmes": {}, "playlists": {}}
    for p in conf.get("programmes") or []:
        need = set()
        for b in (p.get("blocks") or []) if isinstance(p, dict) else []:
            for it in (b.get("items") or []) if isinstance(b, dict) else []:
                k = it.get("kind") if isinstance(it, dict) else None
                if k in ("album", "track"):
                    need.add("ondemand" if it.get("root") == "ondemand" else "music")
                elif k in NEEDS:
                    need.add(NEEDS[k])
        for n in need:
            out["programmes"][n] = out["programmes"].get(n, 0) + 1
    for pl in conf.get("playlists") or []:
        need = {("ondemand" if t[0] == "ondemand" else "music") for t in (pl.get("tracks") or [])
                if isinstance(t, (list, tuple)) and t} if isinstance(pl, dict) else set()
        for n in need:
            out["playlists"][n] = out["playlists"].get(n, 0) + 1
    return out


def _measure(name: str, value) -> dict:
    """How much of a thing there is: {"n": items, "bytes"?: for files}."""
    try:
        m = _as_map(name, value)
    except (TypeError, KeyError, ValueError, AttributeError):
        return {"n": 0}
    out = {"n": len(m)}
    if name.startswith(("media:", "files:")):
        out["bytes"] = sum(_size_of(v) for v in m.values())
    return out


def _as_map(name: str, value) -> dict:
    """A collection as {key: item} (a list's order aside)."""
    key = COLLECTIONS[name][2]
    return {key(x): x for x in value} if key is not None else dict(value)


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


_NONE = object()


def _unset(name: str, key, value) -> bool:
    """Never set on that radio: empty, or (a setting) still what a new radio has."""
    if value is None:
        return True
    if name.startswith("settings"):
        return _canon(value) == _canon(asdict(Settings()).get(key))
    return False


def _pick(name: str, a, b):
    """The one item of two different ones that both radios choose alike: for the media, the smaller file (an MP3
    over the FLAC of the same track); otherwise the larger, as text."""
    if name.startswith("media:"):
        return min(a, b, key=lambda v: (_size_of(v), _canon(v)))
    if name == "places":                    # (the one saved later; the same moment: the further on)
        return max(a, b, key=lambda v: (v.get("at", 0), v.get("pos_ms", 0), _canon(v)))
    return max(a, b, key=_canon)


def merge(name: str, base, mine, theirs) -> tuple[object, dict]:
    """Three-way merge of one collection. base None: the first time (only adds). Returns (the merged
    collection -- a list keeps this radio's order, then theirs' new ones in their order -- and
    {"added", "changed", "deleted": counts against mine})."""
    b = _as_map(name, base) if base is not None else None
    m, t = _as_map(name, mine), _as_map(name, theirs)
    out: dict = {}
    for k in list(m) + [k for k in t if k not in m]:
        mi, ti = m.get(k, _NONE), t.get(k, _NONE)        # (_NONE: not there; None is a value -- a setting left empty)
        bi = b.get(k, _NONE) if b is not None else _NONE
        if b is None:                                    # first time: everything either has, nothing goes
            if ti is _NONE or (mi is not _NONE and _canon(mi) == _canon(ti)):
                got = mi
            elif mi is _NONE:
                got = ti
            elif _unset(name, k, mi) != _unset(name, k, ti):
                got = ti if _unset(name, k, mi) else mi  # (one radio set it, the other never did: the one set wins)
            else:
                got = _pick(name, mi, ti)
        elif mi is not _NONE and ti is not _NONE:
            if _canon(mi) == _canon(ti) or (bi is not _NONE and _canon(ti) == _canon(bi)):
                got = mi                                 # (the same, or only mine changed)
            elif bi is not _NONE and _canon(mi) == _canon(bi):
                got = ti                                 # only theirs changed
            else:
                got = _pick(name, mi, ti)                # both changed it (or both added it): the same rule on every radio
        elif mi is not _NONE:                            # theirs hasn't it
            got = _NONE if bi is not _NONE and _canon(mi) == _canon(bi) else mi    # they deleted it (I hadn't changed it): gone
        else:                                            # mine hasn't it
            got = _NONE if bi is not _NONE and _canon(ti) == _canon(bi) else ti    # I deleted it (they hadn't changed it): stays gone
        if got is not _NONE:
            out[k] = got
    counts = {"added": sum(1 for k in out if k not in m),
              "changed": sum(1 for k in out if k in m and _canon(out[k]) != _canon(m[k])),
              "deleted": sum(1 for k in m if k not in out)}
    merged = list(out.values()) if COLLECTIONS[name][2] is not None else out
    return merged, counts


def same(name: str, a, b) -> bool:
    """The same items (a list's order doesn't count)."""
    return _canon(_as_map(name, a)) == _canon(_as_map(name, b))


# --- One radio's side ---------------------------------------------------------------------------------------------

class Sync:
    """This radio in the sync: its id, the radios it's paired with, what it's heard on the network, and
    the merges. fetch(url, body=None) -> parsed JSON (raises OSError/ValueError): how it talks to another
    radio (a test passes its own). protected() -> True: this radio has a web password (no sync)."""

    def __init__(self, config_file: Path, name: Callable[[], str], port: int, version: str = "",
                 fetch: Callable | None = None, protected: Callable[[], bool] = lambda: False,
                 backup: Callable[[str], None] | None = None, media=None, voice_jobs=None) -> None:
        self.config_file = Path(config_file)
        self.base_file = self.config_file.parent / BASE_FILE
        self.name, self.port, self.version = name, port, version
        self.fetch = fetch or _fetch
        self.protected = protected
        self.backup = backup                     # backup(label): a named settings backup (before a first sync)
        self.apply_live = None                   # apply_live(settings, changed): the running radio takes up a sync's changes
        self.media = media                       # the music storage (media.Media): files synced only with one
        self.get_file = _get_file                # get_file(address, kind, path): a response (.headers, .read) -- a test's own
        self.voice_jobs = voice_jobs             # voices.VoiceJobs: installs a voice fetched from another radio
        self.voice_ok = lambda key, value: True  # voice_ok(setting, voice): this radio has that voice (main: the station's)
        self.wifi_path: Path | None = None       # wifi.json (main sets it on a radio): its networks synced
        self.ask_wifi = wifi_mod.ask             # ask_wifi("reload"): the Wi-Fi manager takes up a new network
        self._sums: dict = {}                    # (kind, path) -> (size, mtime_ns, checksum): files checked once
        self.get_voice = _get_voice              # get_voice(address, name): a response (.headers, .read): the pack, as a tar
        self.seen: dict[str, dict] = {}          # id -> {"id", "name", "address", "version", "at"} (heard lately)
        self.status: dict[str, dict] = {}        # peer id -> {"at", "ok", "said", "held"?}
        self._allow_once: set[str] = set()       # peers whose held merge may go through once
        self._lock = threading.Lock()            # (one merge at a time)
        self._stop = threading.Event()
        self.busy = lambda: False                # busy() -> True: the radio is playing (the media copying waits)
        self.media_status: dict[str, dict] = {}  # peer id -> {"at", "said", "done"?}: how its media copying is going
        self._media_jobs: dict[tuple, tuple] = {}    # (peer id, collection) -> (address, what it was, what it's to be)
        self._media_lock = threading.Lock()
        self._media_worker_on = False
        self.counts: dict[str, dict] = {}        # peer id -> {"at", "here": {name: measure}, "there": {...}, "free_there"}
        self.progress: dict[str, dict] = {}      # peer id -> the exchange going on: {"step", "at", "started"}
        self.copying: dict[str, dict] = {}       # peer id -> the media copy going on: {"step", "name", files, bytes...}
        self.next_at = 0.0                        # when the next round of syncs is due
        self.their_settings: dict[str, dict] = {}  # peer id -> its shared settings at the last exchange (what differs)
        self.places = None                       # the station's book_positions (audiobooks.Positions): places synced
        self.playing_place = lambda: None        # the key of the book playing now (its place isn't moved)
        self.clock_ok = lambda: True             # the clock is set (by NTP): places are synced only then
        self.history: list[dict] = []            # the recent syncs, newest last: {"at", "peer", "ok", "said"} (in memory)
        self._union_seen: dict[str, dict] = {}   # peer id -> {voices/wifi/places: digest both had at the last exchange}
        self._union_sent: dict[str, dict] = {}   # peer id -> the same, as this radio last told it (it asked)
        # A library's file list is a walk of the card (thousands of files: seconds on a Zero, while it plays): kept
        # till the library changes (media.changes) or TREE_MAX_AGE_S. kind -> (changes, when, entries)
        self._trees: dict[str, tuple] = {}
        self._dsums: dict[str, tuple] = {}       # (a library list's fingerprint, kept with the list it's of: _digests())
        self._held_since: dict[str, float] = {}  # peer id -> since when a library list that differs has waited (playing)
        # Files deleted here from the page while a library wasn't agreed with the other radio (a copy still going):
        # left out of what's taken from it, so they don't come back, till the two agree. kind -> {rel: when}
        self._deleted: dict[str, dict] = {}
        self._live: tuple = (0.0, None, {})      # (when, names, collections): this radio's own, read for the window
        conf = self._conf()
        self.id = conf.get("radio_id") if isinstance(conf.get("radio_id"), str) and conf.get("radio_id") else ""
        if not self.id:                          # (made once, kept: who this radio is to the others)
            self.id = uuid.uuid4().hex[:12]
            save_setting(self.config_file, "radio_id", self.id)

    def names(self) -> list[str]:
        """The collections this radio syncs: the files only if it has music storage."""
        return [n for n, c in COLLECTIONS.items() if c[0] is not None or (n == "wifi" and self.wifi_path is not None)
                or (n == "places" and self.places is not None and self.clock_ok())
                or (n == "voices" and self.voice_jobs is not None) or (n.startswith(("files:", "media:")) and self.media is not None)]

    def _peer(self, peer_id: str) -> dict | None:
        return next((p for p in self.peers() if p["id"] == peer_id), None) if peer_id else None

    def share(self, peer_id: str) -> list[str]:
        """What's shared with that paired radio (share keys)."""
        return share_of(self._peer(peer_id))

    def shared(self, peer_id: str) -> list[str]:
        """The media kinds shared with that paired radio (none until both have ticked them)."""
        return [k for k in MEDIA_KINDS if k in self.share(peer_id)]

    def paused(self, peer_id: str) -> float | None:
        """When syncing with that radio was paused (both radios), or None."""
        at = (self._peer(peer_id) or {}).get("paused")
        return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None

    def names_for(self, peer_id: str) -> list[str]:
        """The collections synced with that radio: what's ticked for it (no radio: all but the media)."""
        keys = self.share(peer_id) if peer_id else DEFAULT_SHARE
        allowed = {n for k in keys for n in SHARE[k][1]}
        return [n for n in self.names() if n in allowed]

    def voice_names(self) -> dict:
        """The voice packs here: {name: 1} (a folder with a model; not one part-installed)."""
        root = Path(self.voice_jobs.voices_dir) if self.voice_jobs is not None else None
        if root is None or not root.is_dir():
            return {}
        return {d.name: 1 for d in sorted(root.iterdir())
                if d.is_dir() and not d.name.startswith(".") and (d / "model.onnx").is_file()}

    def _read(self, conf: dict, names) -> dict:
        out = {}
        for n in names:
            read = COLLECTIONS[n][0]
            if read is not None:
                out[n] = read(conf)
            elif n == "voices":
                out[n] = self.voice_names()
            elif n == "wifi":
                out[n] = _check_wifi(wifi_mod.load(self.wifi_path)["networks"]) if self.wifi_path.exists() else []
            elif n == "places":
                out[n] = _clean_places(self.places.snapshot())
            elif n.startswith("media:"):
                kind = n.split(":", 1)[1]
                self._hook_media()
                changes = getattr(self.media, "changes", None)
                ver = changes.get(kind, 0) if isinstance(changes, dict) else None
                kept = self._trees.get(kind)
                if ver is not None and kept and kept[0] == ver and time.time() - kept[1] < TREE_MAX_AGE_S:
                    out[n] = dict(kept[2])
                    continue
                try:
                    if not self.media._root(kind).is_dir():
                        continue                     # (not there: skipped, never read as "everything deleted")
                    out[n] = _media_entries(kind, self.media.tree(kind)["files"])
                except (media_mod.MediaError, OSError):
                    continue
                if ver is not None:
                    self._trees[kind] = (ver, time.time(), dict(out[n]))
            else:
                kind = n.split(":", 1)[1]
                try:
                    if not self.media._root(kind).is_dir():
                        continue                     # (not there: skipped, never read as "everything deleted")
                    out[n] = {path: self._checksum(kind, path, size)
                              for path, size in self.media.tree(kind)["files"].items()}
                except (media_mod.MediaError, OSError):
                    continue
        return out

    def _checksum(self, kind: str, path: str, size: int) -> str:
        """A file's entry: "size:first 16 hex of its SHA-256" -- worked out once, kept while its size and time stay."""
        try:
            full = self.media.resolve(kind, path)
            st = full.stat()
        except (OSError, media_mod.MediaError):
            return f"{size}:{'0' * 16}"
        was = self._sums.get((kind, path))
        if was and was[0] == st.st_size and was[1] == st.st_mtime_ns:
            return was[2]
        h = hashlib.sha256()
        try:
            with open(full, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
        except OSError:
            return f"{size}:{'0' * 16}"
        entry = f"{st.st_size}:{h.hexdigest()[:16]}"
        self._sums[(kind, path)] = (st.st_size, st.st_mtime_ns, entry)
        return entry
        return out

    # (the config, read fresh: other parts of the radio change it)
    def _conf(self) -> dict:
        try:
            return read_config(self.config_file)
        except (OSError, ValueError):
            return {}

    def peers(self) -> list[dict]:
        p = self._conf().get("sync_peers")
        return [x for x in p if isinstance(x, dict) and isinstance(x.get("id"), str)] if isinstance(p, list) else []

    def _save_peers(self, peers: list[dict]) -> None:
        save_setting(self.config_file, "sync_peers", peers)

    def _bases(self) -> dict:
        """What was last agreed with each radio -- only the collections this version has (a file written by an
        older one may name one since renamed: 1.9.97 split "settings" into groups; it's dropped, so that one
        merges as a first sync does)."""
        try:
            b = json.loads(self.base_file.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(b, dict):
            return {}
        return {pid: {n: v for n, v in agreed.items() if n in COLLECTIONS} if isinstance(agreed, dict) else agreed
                for pid, agreed in b.items()}

    def _save_bases(self, bases: dict) -> None:
        write_atomic(self.base_file, json.dumps(bases, ensure_ascii=False) + "\n")

    # --- what's asked of this radio ---

    def hello(self) -> dict:
        return {"sleepradio": PROTOCOL, "id": self.id, "name": self.name(), "port": self.port,
                "version": self.version, "protected": bool(self.protected())}

    def _asked_by_peer(self, for_id: str, ip: str | None) -> bool:
        """The request came from that paired radio, at its own address (Wi-Fi keys go to nobody else)."""
        peer = next((p for p in self.peers() if p["id"] == for_id), None) if for_id else None
        return peer is not None and ip is not None and peer["address"].rsplit(":", 1)[0] == ip

    def state(self, for_id: str = "", ip: str | None = None, digests: bool = False, only=None) -> dict:
        """This radio's collections; and (for_id: the radio asking) a fingerprint of the state this radio last
        agreed on with it -- if the two radios' fingerprints differ (a reply lost mid-sync, a re-pair, a card
        re-flashed), the next merge between them only adds: neither can tell a delete from a missed add.
        digests=True: each collection's fingerprint instead of its contents (and whether this radio is playing),
        so the asking radio fetches only what differs; only: just those collections."""
        conf = self._conf()
        base = self._bases().get(for_id) if for_id else None
        names = [n for n in self.names_for(for_id) if n != "wifi" or self._asked_by_peer(for_id, ip)]
        if only is not None:
            names = [n for n in names if n in only]
        base = base if isinstance(base, dict) else {}
        collections = self._read(conf, names)
        sums = self._digests(collections)
        if for_id:                                   # (what it was told: if it sends nothing back, it had it all)
            self._union_sent.setdefault(for_id, {}).update({n: sums[n] for n in collections if n in UNION_ONLY})
        out = {**self.hello(), "usage": self._usage(),
               "base": self._digests({n: v for n, v in base.items() if n not in UNION_ONLY})}
        if digests:
            return {**out, "digests": sums, "busy": bool(self.busy())}
        return {**out, "collections": collections}

    def _digests(self, collections: dict) -> dict:
        """Each collection's fingerprint. A library's list is thousands of entries (a long, blocking sum on a Zero,
        which starves the sound): its fingerprint is kept while the list is the same."""
        out = {}
        for n, v in collections.items():
            if n.startswith("media:") and isinstance(v, dict):
                kept = self._dsums.get(n)
                if kept is not None and kept[0] == v:
                    out[n] = kept[1]
                    continue
                out[n] = _digest({n: v})
                self._dsums[n] = (dict(v), out[n])
            else:
                out[n] = _digest({n: v})
        return out

    def _hook_media(self) -> None:
        """Be told of deletes and adds made in the library (once: the storage may be given after the start)."""
        m = self.media
        if m is not None and getattr(m, "on_deleted", False) is None:
            m.on_deleted = self._note_delete
        if m is not None and getattr(m, "on_added", False) is None:
            m.on_added = self._note_add

    def _note_delete(self, kind: str, rel: str) -> None:
        if kind not in MEDIA_KINDS or not rel:
            return
        now = time.time()
        tomb = {r: at for r, at in self._deleted.get(kind, {}).items() if now - at < TOMBSTONE_DAYS * 86400}
        tomb[rel] = now
        self._deleted[kind] = tomb

    def _note_add(self, kind: str, rel: str) -> None:
        tomb = self._deleted.get(kind)
        if tomb and rel in tomb:                     # (put back here: no longer a delete to keep)
            del tomb[rel]

    def _without_deleted(self, name: str, entries: dict) -> dict:
        """A library's entries less the ones deleted here while it wasn't agreed (a file, or all in a folder)."""
        tomb = self._deleted.get(name.split(":", 1)[1]) if name.startswith("media:") else None
        if not tomb:
            return entries
        gone = lambda path: any(path == t or path.startswith(t + "/") for t in tomb)
        return {k: v for k, v in entries.items() if not gone(k + str(v).partition(":")[2])}

    def _agreed(self, name: str) -> None:
        """A library agreed with the other radio: its deletes travel by the base from now on."""
        if name.startswith("media:"):
            self._deleted.pop(name.split(":", 1)[1], None)

    def _usage(self) -> dict | None:
        try:
            return self.media.usage() if self.media is not None else None
        except Exception:
            return None

    def paired_with(self, peer: dict) -> None:
        """Another radio paired with this one (its id, name, address): keep it."""
        peers = [p for p in self.peers() if p["id"] != peer["id"]]
        self._save_peers(peers + [{"id": peer["id"], "name": peer.get("name", ""), "address": peer["address"],
                                   "share": list(DEFAULT_SHARE)}])
        log.info("sync: paired with %s (%s)", peer.get("name"), peer["address"])

    def unpaired(self, peer_id: str) -> None:
        self._save_peers([p for p in self.peers() if p["id"] != peer_id])
        bases = self._bases()
        if bases.pop(peer_id, None) is not None:
            self._save_bases(bases)
        self.status.pop(peer_id, None)
        self.media_status.pop(peer_id, None)
        self.history = [h for h in self.history if h["peer"] != peer_id]
        with self._media_lock:
            for key in [k for k in self._media_jobs if k[0] == peer_id]:
                del self._media_jobs[key]
        log.info("sync: unpaired from %s", peer_id)

    # --- what this radio does ---

    def pair(self, address: str) -> dict:
        """Pair with the radio at address ("host:port"): both keep each other, then sync."""
        address = _clean_address(address)
        if self.protected():
            raise ValueError("this radio has a password: it can't be paired")
        them = self.fetch(f"http://{address}/api/sync/hello")
        if not isinstance(them, dict) or them.get("sleepradio") != PROTOCOL or not isinstance(them.get("id"), str):
            raise ValueError("that isn't a Sleep Radio that can sync (or it's a different version of the sync)")
        if them["id"] == self.id:
            raise ValueError("that's this radio")
        if them.get("protected"):
            raise ValueError(f"{them.get('name') or address} has a password: it can't be paired")
        self.fetch(f"http://{address}/api/sync/paired", {"id": self.id, "name": self.name(), "port": self.port})
        self.paired_with({"id": them["id"], "name": them.get("name", ""), "address": address})
        st = self.sync_now(them["id"])
        return {"id": them["id"], "name": them.get("name", ""), "sync": st}

    def unpair(self, peer_id: str) -> None:
        peer = next((p for p in self.peers() if p["id"] == peer_id), None)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        try:
            self.fetch(f"http://{peer['address']}/api/sync/unpaired", {"id": self.id})
        except (OSError, ValueError):
            pass                                  # (it's off: it'll find it's no longer paired when it asks)
        self.unpaired(peer_id)

    # --- what's shared with a radio (set on both at once), pausing, and the media copying ---

    def set_share(self, peer_id: str, keys, paused: bool | None = None) -> dict:
        """From the page: share these things with that paired radio (and pause or resume syncing with it) -- set
        on both radios at once (it must be answering), then a sync straight away. Something newly shared saves a
        settings backup on each radio first, and its first sync only adds; something no longer shared forgets what
        the two agreed on for it (shared again later, it starts the same way)."""
        keys = _check_share(keys)
        peer = self._peer(peer_id)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        if paused is None:
            pause = self.paused(peer_id)
        else:
            pause = (self.paused(peer_id) or time.time()) if paused else None
        name = peer.get("name") or peer["address"]
        try:
            ok = self.fetch(f"http://{peer['address']}/api/sync/share-set",
                            {"id": self.id, "share": keys, "paused": pause})
        except (OSError, ValueError) as e:
            raise ValueError(f"{name} isn't answering ({str(getattr(e, 'reason', e))[:60]}): both radios need "
                             "to be on, with this version or later, to change what they share") from None
        if not (isinstance(ok, dict) and ok.get("ok")):
            raise ValueError(f"{name} didn't take it: {ok.get('error') if isinstance(ok, dict) else 'no answer'}")
        self._apply_share(peer_id, keys, pause)
        if not pause:
            threading.Thread(target=self._quietly, args=(peer_id,), name="sync-now", daemon=True).start()
        return {"ok": True, "share": keys, "paused": pause}

    def share_set(self, peer_id: str, keys, paused, ip: str | None) -> bool:
        """Asked by the paired radio itself (from its own address): what's shared with it, and paused or not."""
        if not self._asked_by_peer(peer_id, ip):
            raise ValueError("only that paired radio can change what it shares")
        pause = paused if isinstance(paused, (int, float)) and not isinstance(paused, bool) and paused > 0 else None
        self._apply_share(peer_id, _check_share(keys), pause)
        return True

    def share_media(self, peer_id: str, kinds) -> dict:
        """The media kinds shared with that radio (the rest as it is)."""
        kinds = _check_kinds(kinds)
        return self.set_share(peer_id, [k for k in self.share(peer_id) if k not in MEDIA_KINDS] + kinds)

    def media_set(self, peer_id: str, kinds, ip: str | None) -> bool:
        """Asked by a paired radio on 1.9.95-96: its media ticks (the rest as it is)."""
        kinds = _check_kinds(kinds)
        return self.share_set(peer_id, [k for k in self.share(peer_id) if k not in MEDIA_KINDS] + kinds,
                              self.paused(peer_id), ip)

    def _apply_share(self, peer_id: str, keys: list[str], paused: float | None) -> None:
        with self._lock:
            peer = self._peer(peer_id)
            if peer is None:
                raise ValueError("that radio isn't paired with this one")
            was = set(share_of(peer))
            if peer.get("paused") and not paused:    # (resumed: the "paused" left from the last try isn't news)
                self.status[peer_id] = {"at": time.time(), "ok": True, "said": "syncing again"}
            added = [k for k in keys if k not in was]
            if added and self.backup is not None:     # (something newly shared: a backup first, as before a first sync)
                try:
                    self.backup(f"Before sharing {', '.join(SHARE[k][0] for k in added)} with "
                                f"{peer.get('name') or 'another radio'}"[:120])
                except Exception:
                    log.exception("sync: the backup before sharing more wasn't made")
            self._save_peers([{**{k: v for k, v in p.items() if k not in ("media", "paused")}, "share": keys,
                               **({"paused": paused} if paused else {})} if p["id"] == peer_id else p
                              for p in self.peers()])
            dropped = {n for k in was - set(keys) for n in SHARE[k][1]}
            if dropped:
                bases = self._bases()
                b = bases.get(peer_id)
                if isinstance(b, dict) and dropped & set(b):
                    bases[peer_id] = {n: v for n, v in b.items() if n not in dropped}
                    self._save_bases(bases)
            with self._media_lock:                   # (no longer shared, or paused: its copying stops)
                for key in [k for k in self._media_jobs if k[0] == peer_id and (k[1] in dropped or paused)]:
                    del self._media_jobs[key]
        if not any(k in MEDIA_KINDS for k in keys):
            self.media_status.pop(peer_id, None)
        log.info("sync: shared with %s: %s%s", peer_id, ", ".join(keys) or "nothing", " (paused)" if paused else "")

    def _queue_media(self, peer_id: str, address: str, name: str, mine: dict, want: dict) -> None:
        """Copy what a sync agreed on, in the background (a newer sync's goal replaces an older one's)."""
        with self._media_lock:
            self._media_jobs[(peer_id, name)] = (address, mine, want)
            if self._media_worker_on:
                return
            self._media_worker_on = True
        threading.Thread(target=self._media_worker, name="sync-media", daemon=True).start()

    def _media_worker(self) -> None:
        while not self._stop.is_set():
            with self._media_lock:
                if not self._media_jobs:
                    self._media_worker_on = False
                    return
                key = next(iter(self._media_jobs))
                job = self._media_jobs[key]
            try:
                self._media_job(key[0], key[1], *job)
            except Exception:
                log.exception("sync: copying the %s failed", WORDS.get(key[1], key[1]))
            with self._media_lock:
                if self._media_jobs.get(key) is job:     # (not replaced by a newer one meanwhile)
                    del self._media_jobs[key]
        with self._media_lock:
            self._media_worker_on = False

    def _media_said(self, peer_id: str, said: str, done: bool = False) -> None:
        self.media_status[peer_id] = {"at": time.time(), "said": said, **({"done": True} if done else {})}

    def _media_job(self, peer_id: str, name: str, address: str, mine: dict, want: dict) -> bool:
        """Bring one library to what the sync agreed: delete what went (only files that were here when it was
        worked out: one added since is left alone), check there's room, then fetch what's missing or different,
        one file at a time, waiting while the radio plays. All there: kept as agreed (the base). True if so."""
        kind, words = name.split(":", 1)[1], WORDS[name]
        key = (peer_id, name)
        superseded = lambda: self._media_jobs.get(key, (None, None, None))[2] is not want or bool(self.paused(peer_id))
        goal, before = _media_paths(want), _media_paths(mine)
        try:
            have = {p: s for p, s in self.media.tree(kind)["files"].items() if _syncable(kind, p)}
        except (media_mod.MediaError, OSError) as e:
            self._media_said(peer_id, f"{words}: couldn't look at the library ({e})")
            return False
        gone = [p for p in before if p not in goal and p in have]
        fetch = sorted(p for p, size in goal.items() if have.get(p) != size)
        need = sum(goal[p] - have.get(p, 0) for p in fetch) - sum(have[p] for p in gone)
        room = self.media.usage()["free"] - media_mod.KEEP_FREE
        if need > room:
            self._media_said(peer_id, f"{words}: held, {(need - room) / 1e9:.1f} GB more room needed on this radio "
                                      f"for {len(fetch)} files. Free some, and it carries on")
            log.info("sync: %s held: %d bytes more needed", words, need - room)
            return False
        ok = True
        try:
            for p in gone:
                try:
                    self._delete_by_sync(kind, p, prune=True)
                except media_mod.MediaError as e:
                    ok = False
                    log.warning("sync: %s/%s not deleted (%s)", kind, p, e)
            left = sum(goal[p] for p in fetch)
            prog = {"step": "copying", "name": name, "label": LABELS.get(name, words), "files": len(fetch),
                    "files_done": 0, "bytes": left, "bytes_done": 0, "started": time.time(), "at": time.time()}
            if fetch:
                self.copying[peer_id] = prog
            for i, p in enumerate(fetch):
                while self.busy() and not self._stop.is_set() and not superseded():
                    self._media_said(peer_id, f"{words}: {len(fetch) - i} to copy ({left / 1e9:.2f} GB): "
                                              "waiting while the radio plays")
                    prog.update(step="waiting", at=time.time())
                    self._stop.wait(MEDIA_WAIT_S)
                prog.update(step="copying", files_done=i, at=time.time())
                if self._stop.is_set() or superseded():
                    return False
                self._media_said(peer_id, f"{words}: copying {i + 1} of {len(fetch)} ({left / 1e9:.2f} GB to go)")
                size = goal[p]
                try:
                    with self.get_file(address, kind, p) as r:
                        length = int(r.headers.get("Content-Length") or 0)
                        if length != size:
                            raise OSError(f"{length} bytes, not {size}")
                        if p in have:                     # (a different one there: out of the way first)
                            self._delete_by_sync(kind, p)

                        def read(n, r=r):
                            data = r.read(n)
                            prog["bytes_done"] += len(data)
                            prog["at"] = time.time()
                            return data
                        self.media.receive(kind, "", p, length, read, replace=True)
                except (OSError, ValueError, media_mod.MediaError) as e:
                    ok = False
                    log.warning("sync: %s/%s not fetched (%s)", kind, p, e)
                left -= size
        finally:
            if self.copying.get(peer_id) is prog:
                self.copying.pop(peer_id, None)
            if gone or fetch:
                try:
                    self.media.done()
                except Exception:
                    log.exception("sync: the music storage wasn't put back")
        if not ok:
            self._media_said(peer_id, f"{words}: some didn't copy: it tries again at the next sync")
            return False
        with self._lock:
            b = self._bases().get(peer_id)
            self._keep_base(peer_id, {**(b if isinstance(b, dict) else {}), name: want})
            self._agreed(name)
            c = self.counts.get(peer_id)
            if c:
                c["here"][name] = _measure(name, want)
            self._live = (0.0, None, {})
        self._media_said(peer_id, f"{words}: the same on both" + (f" ({len(fetch)} copied, {len(gone)} deleted)"
                                                                     if fetch or gone else ""), done=True)
        if fetch or gone:
            self._remember(peer_id, True, f"{words}: {len(fetch)} copied here, {len(gone)} deleted here")
        log.info("sync: %s: %d copied, %d deleted", words, len(fetch), len(gone))
        return True

    def _delete_by_sync(self, kind: str, path: str, prune: bool = False) -> None:
        try:
            self.media.delete(kind, path, prune=prune, by_sync=True)
        except TypeError:                            # (a storage without by_sync: a test's)
            self.media.delete(kind, path, prune=prune)

    def allow_once(self, peer_id: str) -> None:
        self._allow_once.add(peer_id)

    def sync_now(self, peer_id: str) -> dict:
        return self.pull(peer_id, now=True)         # (asked for: the library lists too, playing or not)

    def pull(self, peer_id: str, now: bool = False) -> dict:
        """One exchange with a paired radio: read its state, merge it with this radio's against the last
        state the two agreed on (the base), send it the result to take up (only if it hasn't changed
        meanwhile), then take it up here too: both end the same, and both keep it as the new base.
        First only the fingerprints: what's the same on both (and agreed) isn't sent or merged at all, and a
        library list that differs waits while either radio plays (MEDIA_HOLD_S at most; now: not at all) --
        a big exchange stalls the sound on a Zero."""
        peer = next((p for p in self.peers() if p["id"] == peer_id), None)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        if self.protected():
            return self._said(peer_id, False, "this radio has a password: no sync")
        if self.paused(peer_id):
            return self._said(peer_id, False, "paused: Resume to sync again", paused=True)
        self._step(peer_id, "asking")

        def ask(query: str):
            try:
                them = self.fetch(f"http://{peer['address']}/api/sync/state?for={self.id}{query}")
            except (OSError, ValueError) as e:
                return None, self._said(peer_id, False, f"not answering ({str(getattr(e, 'reason', e))[:60]})")
            if not isinstance(them, dict) or them.get("id") != peer_id:
                return None, self._said(peer_id, False, "a different radio is at that address now")
            if them.get("sleepradio") != PROTOCOL:
                return None, self._said(peer_id, False, "it has a different version of the sync: update both radios")
            if them.get("protected"):
                return None, self._said(peer_id, False, "it has a password: no sync")
            return them, None

        them, failed = ask("&digests=1")
        if failed:
            return failed
        keep: set = set()                            # (the same on both and agreed: not sent, not merged; its base kept)
        waits: list = []                             # (library lists that differ, waiting while a radio plays)
        if isinstance(them.get("digests"), dict):
            sums = them["digests"]
            names = [n for n in self.names_for(peer_id) if n in sums]
            with CONFIG_LOCK:
                mine_d = self._digests(self._read(read_config(self.config_file), names))
            kept = self._bases().get(peer_id)
            kept = kept if isinstance(kept, dict) else {}
            kept_d = self._digests({n: kept[n] for n in names if n in kept and n not in UNION_ONLY})
            their_base = them.get("base") if isinstance(them.get("base"), dict) else {}
            keep = {n for n in names if n in mine_d and mine_d[n] == sums[n]
                    and (n in UNION_ONLY or kept_d.get(n) == sums[n] == their_base.get(n))}
            differ = [n for n in names if n.startswith("media:") and n not in keep]
            since = self._held_since.get(peer_id)
            if differ and not now and (self.busy() or them.get("busy")) and (since is None or time.time() - since < MEDIA_HOLD_S):
                waits = differ
                self._held_since.setdefault(peer_id, time.time())
            else:
                self._held_since.pop(peer_id, None)
            want = [n for n in names if n not in keep and n not in waits]
            if keep & UNION_ONLY:
                self._union_seen.setdefault(peer_id, {}).update({n: mine_d[n] for n in keep & UNION_ONLY})
            if not want:
                said = "already the same" + (" (the music lists wait till neither radio is playing)" if waits else "")
                log.info("sync with %s: %s", them.get("name") or peer_id, said)
                self._step(peer_id, "comparing")
                return self._said(peer_id, True, said)
            them, failed = ask("&only=" + ",".join(want))
            if failed:
                return failed
            keep |= set(waits)
        try:
            names = [n for n in self.names_for(peer_id) if n in them["collections"] and n not in keep]     # (what both sync)
            theirs = {n: COLLECTIONS[n][1](them["collections"][n]) for n in names}
        except (KeyError, TypeError, ValueError) as e:
            return self._said(peer_id, False, f"what it sent didn't check out ({e})")
        their_name = str(them.get("name") or "")
        self._step(peer_id, "comparing")
        with self._lock:
            kept = self._bases().get(peer_id)
            kept = kept if isinstance(kept, dict) else {}
            theirs_base = them.get("base") if isinstance(them.get("base"), dict) else {}
            # (each collection's base: only where both radios agree on it -- elsewhere (a reply lost mid-sync, a file
            # that didn't arrive, a re-pair) that one merges as a first sync does: adds only, never a delete)
            base = {n: kept[n] for n in names if n in kept and n not in UNION_ONLY
                    and theirs_base.get(n) == self._digests({n: kept[n]})[n]}
            if len(base) < len([n for n in names if n not in UNION_ONLY]) and kept:
                log.info("sync with %s: only adds this time for %s", their_name,
                         ", ".join(WORDS[n] for n in names if n not in base and n not in UNION_ONLY))
            if not kept and self.backup is not None:     # (the first sync with that radio: a backup first)
                try:
                    self.backup(f"Before syncing with {their_name or peer.get('name') or 'another radio'}")
                except Exception:
                    log.exception("sync: the backup before the first sync wasn't made")
            with CONFIG_LOCK:                             # (worked out under the lock; the other radio asked without it)
                conf = read_config(self.config_file)
                mine = self._read(conf, names)
                names = [n for n in names if n in mine]  # (one this radio can't read now: left alone this time)
                theirs = {n: theirs[n] for n in names}
                merged, here, there, held = {}, {}, {}, []
                for n in names:
                    check = COLLECTIONS[n][1]
                    take = theirs[n] if n in base else self._without_deleted(n, theirs[n])   # (deleted here: not taken back)
                    merged[n], here[n] = merge(n, base.get(n), mine[n], take)
                    there[n] = _gone(n, theirs[n], merged[n])                  # (what the other radio would lose)
                    for where, count, of in (("here", here[n]["deleted"], mine[n]), ("there", there[n], theirs[n])):
                        if count > HOLD_MIN and count > HOLD_DELETES * max(1, len(_as_map(n, of))) \
                                and peer_id not in self._allow_once:
                            held.append(f"{count} of {len(_as_map(n, of))} {WORDS[n]} {where}")
                    try:
                        merged[n] = check(merged[n])
                    except ValueError as e:                # (e.g. more stations together than a radio keeps)
                        return self._said(peer_id, False, f"together they don't fit: {e}")
            self._keep_counts(peer_id, mine, theirs, them.get("usage"), keep=keep)
            self.their_settings[peer_id] = {**{n: v for n, v in (self.their_settings.get(peer_id) or {}).items() if n in keep},
                                            **{n: v for n, v in theirs.items() if n.startswith("settings:")}}
            if held:
                return self._said(peer_id, False, "held: it would delete " + ", ".join(held) +
                                  ". Sync anyway to let it through", held=True)
            if not all(same(n, merged[n], theirs[n]) for n in names) \
                    or not all(n in base and same(n, base[n], merged[n]) for n in names if n not in UNION_ONLY):
                self._step(peer_id, "sending")
                try:                                       # (the other radio takes it up, if it hasn't changed since)
                    ok = self.fetch(f"http://{peer['address']}/api/sync/adopt",
                                    {"id": self.id, "merged": merged, "was": _digest(_plain_of(theirs)),
                                     "was_media": {n: _digest({n: v}) for n, v in theirs.items() if n.startswith("media:")},
                                     "counts": {n: _measure(n, v) for n, v in mine.items()}, "usage": self._usage()})
                except (OSError, ValueError) as e:
                    return self._said(peer_id, False, f"it didn't take the sync ({str(getattr(e, 'reason', e))[:60]})")
                if not (isinstance(ok, dict) and ok.get("ok")):
                    return self._said(peer_id, False, "it changed meanwhile: it'll sync next time")
            with CONFIG_LOCK:
                conf = read_config(self.config_file)
                now = self._read(conf, names)
                if _digest(_plain_of(now)) != _digest(_plain_of(mine)):    # (changed here while it was asked: kept; the next sync merges it)
                    return self._said(peer_id, False, "changed here meanwhile: it'll sync next time")
                updates = self._write(conf, merged, mine)
            # (a library that changed here meanwhile -- a copy still going, say -- is left to the next sync: the
            # rest of the sync isn't held up by it)
            moved = {n for n in names if n.startswith("media:") and _digest({n: now.get(n)}) != _digest({n: mine[n]})}
            got = self._files(peer["address"], [n for n in names if n not in moved], mine, merged, peer_id)
            self._keep_base(peer_id, {**{n: v for n, v in kept.items() if n in keep},
                                      **{n: v for n, v in merged.items() if n in got or not n.startswith(("files:", "media:"))}})
            for n in got:
                self._agreed(n)
            self._allow_once.discard(peer_id)
            # (now: both hold what was merged -- the files where they've arrived; the rest as they were read)
            files_ = lambda n: n.startswith(("files:", "media:"))
            self._union_seen.setdefault(peer_id, {}).update({n: _digest({n: merged[n]}) for n in names if n in UNION_ONLY})
            self._keep_counts(peer_id, {n: merged[n] if n in got or not files_(n) else mine[n] for n in names},
                              {n: theirs[n] if files_(n) else merged[n] for n in names}, them.get("usage"), keep=keep)
        if their_name and their_name != peer.get("name"):
            self._save_peers([{**p, "name": their_name} if p["id"] == peer_id else p for p in self.peers()])
        words = lambda counts: ", ".join(f"{v} {k} {WORDS[n]}" for n, c in counts.items() for k, v in c.items() if v)
        here_w, there_w = words(here), words({n: _changes(n, theirs[n], merged[n]) for n in names})
        said = "; ".join(x for x in (here_w and f"here: {here_w}", there_w and f"on {their_name or 'it'}: {there_w}") if x)
        log.info("sync with %s: %s", their_name or peer_id, said or "already the same")
        return self._said(peer_id, True, said or "already the same")

    def adopt(self, peer_id: str, merged: dict, was: str, ip: str | None = None, was_media: dict | None = None,
              counts: dict | None = None, usage: dict | None = None) -> bool:
        """Asked by a paired radio: take up the merged collections it worked out -- if this radio's are still
        what it read (was: their digest); else no (it syncs again later). Kept as the base with it too."""
        if not any(p["id"] == peer_id for p in self.peers()):
            raise ValueError("that radio isn't paired with this one")
        if not isinstance(merged, dict):
            raise ValueError("send the merged collections")
        if self.paused(peer_id):
            return False
        names = [n for n in self.names_for(peer_id) if n in merged and (n != "wifi" or self._asked_by_peer(peer_id, ip))]
        new = {n: COLLECTIONS[n][1](merged[n]) for n in names}      # (ValueError: refused)
        if not self._lock.acquire(timeout=2):        # (this radio is syncing with that one itself: no -- try later)
            return False
        try:
            if not self._bases().get(peer_id) and self.backup is not None:     # (its first sync with that radio: a backup first)
                peer = next(p for p in self.peers() if p["id"] == peer_id)
                try:
                    self.backup(f"Before syncing with {peer.get('name') or 'another radio'}")
                except Exception:
                    log.exception("sync: the backup before the first sync wasn't made")
            with CONFIG_LOCK:
                conf = read_config(self.config_file)
                mine = self._read(conf, names)
                if set(mine) != set(names) or _digest(_plain_of(mine)) != was:
                    return False
                # (a library that changed here since the other radio read it -- a copy going on -- isn't taken up
                # this time: the next sync merges it as it is then)
                was_media = was_media if isinstance(was_media, dict) else {}
                skip = {n for n in names if n.startswith("media:") and was_media.get(n) != _digest({n: mine[n]})}
                names = [n for n in names if n not in skip]
                new, mine = {n: new[n] for n in names}, {n: mine[n] for n in names}
                new = {n: self._without_deleted(n, v) if n.startswith("media:") else v for n, v in new.items()}
                updates = self._write(conf, new, mine)
            files = [n for n in names if (n.startswith(("files:", "media:")) or n == "voices") and not same(n, new[n], mine[n])]
            old = self._bases().get(peer_id)             # (what it didn't send was the same on both: its base stays)
            rest = {n: v for n, v in (old if isinstance(old, dict) else {}).items() if n not in merged}
            self._keep_base(peer_id, {**rest, **{n: v for n, v in new.items() if n not in files}})     # (those once they're here)
            self.status[peer_id] = {"at": time.time(), "ok": True, "said": "synced (it asked)"}
            self._union_seen.setdefault(peer_id, {}).update({n: _digest({n: new[n]}) for n in names if n in UNION_ONLY})
            ch = ", ".join(f"{v} {k} {WORDS[n]}" for n in names for k, v in _changes(n, mine[n], new[n]).items() if v)
            peer_name = (self._peer(peer_id) or {}).get("name") or "it"
            self._remember(peer_id, True, f"{peer_name} synced: " + (f"here: {ch}" if ch else "already the same"))
            sent = counts if isinstance(counts, dict) else {}
            self._keep_counts(peer_id, {n: new[n] if n not in files else mine[n] for n in names}, None,
                              usage if isinstance(usage, dict) else None, keep=set(rest),
                              there={n: _measure(n, new[n]) if not n.startswith(("files:", "media:"))
                                     else _clean_measure(sent.get(n), _measure(n, new[n])) for n in names})
        finally:
            self._lock.release()
        if files:                                    # (the files after, in the background: the other radio's waiting)
            peer = next(p for p in self.peers() if p["id"] == peer_id)
            threading.Thread(target=self._adopt_files, args=(peer_id, peer["address"], files, mine, new),
                             name="sync-files", daemon=True).start()
        return True

    def _adopt_files(self, peer_id, address, names, mine, new) -> None:
        with self._lock:
            got = self._files(address, names, mine, new, peer_id)
            self._keep_base(peer_id, {**(self._bases().get(peer_id) or {}), **{n: new[n] for n in names if n in got}})

    def _files(self, address: str, names, mine: dict, want: dict, peer_id: str = "") -> set:
        """Bring this radio's files to want: fetch what's missing or different from the radio at address,
        delete what's gone. Returns the file collections now exactly as wanted (the rest: kept out of the
        base, so the next sync only adds -- nothing is deleted for a file that didn't arrive)."""
        done = set()
        if "voices" in names and self.voice_jobs is not None:
            missing = [v for v in want.get("voices", {}) if v not in mine.get("voices", {})]
            for name in missing[:1]:                 # (one voice a sync: an install takes a while; the next sync, the next)
                try:
                    with self.get_voice(address, name) as r:
                        length = int(r.headers.get("Content-Length") or 0)
                        self.voice_jobs.receive(name, f"{name}.tar", length, r.read)
                    log.info("sync: voice %s fetched; installing", name)
                except (OSError, ValueError) as e:
                    log.warning("sync: voice %s not fetched (%s)", name, e)
            if not missing:
                done.add("voices")
        for n in names:
            if n.startswith("media:") and self.media is not None:
                if same(n, want[n], mine.get(n, {})):
                    done.add(n)                      # (nothing to copy: agreed now)
                else:
                    self._queue_media(peer_id, address, n, mine.get(n, {}), want[n])
                continue
            if not n.startswith("files:") or self.media is None:
                continue
            kind, ok = n.split(":", 1)[1], True
            have, goal = mine.get(n, {}), want[n]
            try:
                for path, entry in goal.items():
                    if have.get(path) == entry:
                        continue
                    size = _size_of(entry)
                    try:
                        with self.get_file(address, kind, path) as r:
                            length = int(r.headers.get("Content-Length") or 0)
                            if length != size:
                                raise OSError(f"{path}: {length} bytes, not {size}")
                            if path in have:             # (a different one there: out of the way first -- the storage
                                self.media.delete(kind, path)    # keeps a file of the same size as "the same")
                            self.media.receive(kind, "", path, length, r.read, replace=True)
                        self._sums.pop((kind, path), None)
                        if isinstance(entry, str) and self._checksum(kind, path, size) != entry:
                            raise OSError(f"{path}: arrived, but not as it is there")
                    except (OSError, ValueError, media_mod.MediaError) as e:
                        ok = False
                        log.warning("sync: %s/%s not fetched (%s)", kind, path, e)
                for path in have:
                    if path not in goal:
                        try:
                            self.media.delete(kind, path, prune=True)
                        except media_mod.MediaError as e:
                            ok = False
                            log.warning("sync: %s/%s not deleted (%s)", kind, path, e)
            finally:
                try:
                    self.media.done()
                except Exception:
                    log.exception("sync: the music storage wasn't put back")
            if ok:
                done.add(n)
        return done

    def _write(self, conf: dict, merged: dict, mine: dict) -> dict:
        """Write the collections that changed into the config (under CONFIG_LOCK), then take them up in the
        running radio (apply_live: set by the web server). Returns the config keys written."""
        updates: dict = {}
        for n in merged:
            c = COLLECTIONS[n]
            value = merged[n]
            if c[3] is not None and not same(n, value, mine[n]):
                out = c[3]({**conf, **updates}, value)
                updates.update(out)
        if "wifi" in merged and self.wifi_path is not None and not same("wifi", merged["wifi"], mine.get("wifi", [])):
            self._write_wifi(merged["wifi"])
        if "places" in merged and self.places is not None and not same("places", merged["places"], mine.get("places", {})):
            n = self.places.take(merged["places"], skip=self.playing_place())
            if n:
                log.info("sync: %d places in books and podcasts taken", n)
        if not updates:
            return {}
        before = {k: conf.get(k) for k in updates}
        conf.update(updates)
        write_atomic(self.config_file, json.dumps(conf, indent=2) + "\n")
        changed = {k for k, v in updates.items() if _canon(before[k]) != _canon(v)}
        if changed and self.apply_live is not None:
            threading.Thread(target=self._take_up, args=(updates, changed), name="sync-apply", daemon=True).start()
        return updates

    def _write_wifi(self, networks: list[dict]) -> None:
        """Networks another radio has, added after this radio's own (never one taken away)."""
        data = wifi_mod.load(self.wifi_path)
        have = {n["ssid"] for n in data["networks"]}
        new = [n for n in networks if n["ssid"] not in have]
        if not new:
            return
        data["networks"] = data["networks"] + new
        wifi_mod.save(data, self.wifi_path)
        log.info("sync: Wi-Fi networks added: %s", ", ".join(n["ssid"] for n in new))
        t = threading.Timer(15, self._reload_wifi)   # (after the exchange: a reconfigure may drop the link a moment)
        t.daemon = True
        t.start()

    def _reload_wifi(self) -> None:
        try:
            self.ask_wifi("reload")
        except OSError as e:
            log.warning("sync: the Wi-Fi manager wasn't asked to reload (%s)", e)

    def _take_up(self, updates: dict, changed: set) -> None:
        later = {k for k in changed & VOICE_SETTINGS if not self.voice_ok(k, updates.get(k))}
        if later:                                    # (a voice not here yet: kept in the settings, taken up once it is)
            log.info("sync: %s waits for its voice", ", ".join(sorted(later)))
            changed = changed - later
            if not changed:
                return
        try:
            self.apply_live(updates, changed)
            return
        except Exception as e:
            log.warning("sync: taking up %s together failed (%s): one at a time", ", ".join(sorted(changed)), e)
        for key in sorted(changed):                  # (one that can't be taken up mustn't stop the rest)
            try:
                self.apply_live({k: v for k, v in updates.items() if k == key or k not in changed}, {key})
            except Exception:
                log.exception("sync: the radio couldn't take up %s", key)

    def _keep_base(self, peer_id: str, agreed: dict) -> None:
        agreed = {n: v for n, v in agreed.items() if n not in UNION_ONLY}     # (never Wi-Fi keys in the base file)
        bases = self._bases()
        old = bases.get(peer_id)
        if isinstance(old, dict) and set(old) == set(agreed) and all(same(n, old[n], agreed[n]) for n in agreed):
            return                                         # (unchanged: not written -- few writes to the card)
        bases[peer_id] = agreed
        self._save_bases(bases)

    def _step(self, peer_id: str, step: str) -> None:
        was = self.progress.get(peer_id) or {}
        now = time.time()
        busy = was.get("step") in ("asking", "comparing", "sending")
        self.progress[peer_id] = {"step": step, "at": now, "started": was.get("started", now) if busy else now}

    def _keep_counts(self, peer_id: str, here: dict, theirs: dict | None, usage: dict | None,
                     there: dict | None = None, keep=frozenset()) -> None:
        """What each radio holds, as of this exchange (shown in Other radios); keep: collections not exchanged
        this time (the same on both), their counts as they were."""
        self._live = (0.0, None, {})                  # (this radio's own: read again for the window)
        was = self.counts.get(peer_id) or {}
        old = lambda side: {n: v for n, v in (was.get(side) or {}).items() if n in keep}
        self.counts[peer_id] = {"at": time.time(), "here": {**old("here"), **{n: _measure(n, v) for n, v in here.items()}},
                                "there": {**old("there"), **(there if there is not None else {n: _measure(n, v) for n, v in (theirs or {}).items()})},
                                "free_there": usage.get("free") if isinstance(usage, dict) else None}

    HISTORY = 20

    def _remember(self, peer_id: str, ok: bool, said: str) -> None:
        """A sync for the window's Recent syncs (in memory only: no writes to the card). A run of the same outcome
        (already the same, every few minutes) is one entry, its time the latest."""
        last = next((h for h in reversed(self.history) if h["peer"] == peer_id), None)
        if last is not None and last["ok"] == ok and last["said"] == said:
            last["at"], last["times"] = time.time(), last.get("times", 1) + 1
            return
        self.history.append({"at": time.time(), "peer": peer_id, "ok": ok, "said": said})
        del self.history[:-self.HISTORY * 4]

    def _said(self, peer_id: str, ok: bool, said: str, held: bool = False, paused: bool = False) -> dict:
        st = {"at": time.time(), "ok": ok, "said": said, **({"held": True} if held else {}),
              **({"paused": True} if paused else {})}
        self._remember(peer_id, ok, said)
        self.status[peer_id] = st
        self.progress[peer_id] = {"step": "done" if ok else "failed", "at": st["at"]}
        if not ok:
            log.info("sync: %s: %s", peer_id, said)
        return st

    def progress_for(self, peer_id: str) -> dict:
        """What's going on with that radio, as this one sees it (asked by it, to show on its own page)."""
        return {"exchange": self.progress.get(peer_id), "copying": self.copying.get(peer_id),
                "media_status": self.media_status.get(peer_id)}

    def busy_summary(self) -> dict | None:
        """For the page's status: a sync or a copy going on (the Other radios icon shows it)."""
        now = time.time()
        for c in list(self.copying.values()):
            return {"step": c["step"], "pct": int(100 * c["bytes_done"] / c["bytes"]) if c.get("bytes") else None}
        for p in list(self.progress.values()):
            if p.get("step") in ("asking", "comparing", "sending") and now - p["at"] < 120:
                return {"step": p["step"], "pct": None}
        return None

    def _here_now(self, names: list[str]) -> dict:
        """This radio's own collections, read at most every 15 s for the window (a library is a walk of the card)."""
        at, was_names, got = self._live
        if time.time() - at > 15 or was_names != tuple(names):
            try:
                got = self._read(self._conf(), names)
            except Exception:
                got = {}
            self._live = (time.time(), tuple(names), got)
        return got

    def items(self, peer_id: str) -> list[dict]:
        """Everything that can be shared with that radio, how much each radio has, and whether they're the same:
        [{"name", "label", "shared", "here", "there", "state"}] -- state: same, copying, waiting, held, changed
        (here, since the last sync), unsynced (not agreed yet), differs, off (not shared)."""
        shared = self.names_for(peer_id)
        allnames = [n for n in LABELS if n in self.names() and (n != "wifi" or self.wifi_path is not None)]
        live = self._here_now(shared)
        bases = self._bases().get(peer_id)
        bases = bases if isinstance(bases, dict) else {}
        c = self.counts.get(peer_id) or {"here": {}, "there": {}}
        job = self.copying.get(peer_id) or {}
        said = str((self.media_status.get(peer_id) or {}).get("said", ""))
        out = []
        for n in allnames:
            here = _measure(n, live[n]) if n in live else c["here"].get(n)
            there = c["there"].get(n)
            if n not in shared:
                state = "off"
            elif job.get("name") == n:
                state = job["step"]
            elif n.startswith("media:") and said.startswith(WORDS[n] + ": held"):
                state = "held"
            elif n in UNION_ONLY:
                seen = [d.get(n) for d in (self._union_seen.get(peer_id), self._union_sent.get(peer_id)) if d and d.get(n)]
                if seen and n in live:
                    state = "same" if _digest({n: live[n]}) in seen else "changed"
                else:
                    state = "same" if here and there and here["n"] == there["n"] else "differs"
            elif n in bases and n in live:
                state = "same" if same(n, bases[n], live[n]) else "changed"
            else:
                state = "unsynced"
            out.append({"name": n, "label": LABELS[n], "shared": n in shared, "here": here, "there": there,
                        "state": state})
        return out

    def rows(self, peer_id: str) -> list[dict]:
        """The window's table: one row per thing that can be shared -- [{"key", "label", "shared", "state",
        "members": items(), "warnings": [...], "differs": [...]}]; state: the member's that matters most."""
        items = {i["name"]: i for i in self.items(peer_id)}
        keys = self.share(peer_id)
        peer = self._peer(peer_id) or {}
        who = peer.get("name") or "the other radio"
        needs = _needs(self._conf())
        theirs = self.their_settings.get(peer_id) or {}
        live = self._live[2] if isinstance(self._live[2], dict) else {}
        order = ("copying", "waiting", "held", "differs", "changed", "unsynced", "same", "off")
        out = []
        for k, (label, names) in SHARE.items():
            members = [items[n] for n in names if n in items]
            if not members:
                continue
            states = [m["state"] for m in members]
            state = next((s for s in order if s in states), "off") if k in keys else "off"
            warnings = []
            if k in keys and k in needs:
                what = "programme" if k == "programmes" else "playlist"
                for need, n in sorted(needs[k].items()):
                    if need not in keys:
                        warnings.append(f"{n} {what}{'s' if n != 1 else ''} here use{'s' if n == 1 else ''} "
                                        f"{SHARE[need][0]}, which isn't shared with {who}: there "
                                        f"{'it' if n == 1 else 'they'} may find nothing to play")
            differs = []
            if k in keys and k.startswith("set:") and state != "same":
                n = SHARE[k][1][0]
                mine, there = live.get(n), theirs.get(n)
                if isinstance(mine, dict) and isinstance(there, dict):
                    for s in sorted(set(mine) | set(there)):
                        if _canon(mine.get(s)) != _canon(there.get(s)):
                            differs.append(f"{s.replace('_', ' ')}: {mine.get(s)!r} here, {there.get(s)!r} on {who}")
            out.append({"key": k, "label": label, "shared": k in keys, "state": state, "members": members,
                        "warnings": warnings, "differs": differs[:8]})
        return out

    def overview(self, live: bool = False) -> dict:
        """The Other radios window. live: the items too, and each paired radio on the network asked how its side
        of a sync or a copy is going."""
        now = time.time()
        peers = self.peers()
        paired = {p["id"] for p in peers}
        online = lambda p: p["id"] in self.seen and now - self.seen[p["id"]]["at"] < SEEN_FOR_S
        theirs = {}
        if live:
            for p in peers:
                if online(p):
                    try:
                        theirs[p["id"]] = self.fetch(f"http://{p['address']}/api/sync/progress?for={self.id}")
                    except (OSError, ValueError):
                        pass
        usage = self._usage()
        return {"me": {**self.hello(), "free": usage.get("free") if usage else None},
                "next_at": self.next_at or None,
                "peers": [{**p, "online": online(p), "status": self.status.get(p["id"]), "media": self.shared(p["id"]),
                           "share": self.share(p["id"]), "paused": self.paused(p["id"]),
                           "history": [h for h in self.history if h["peer"] == p["id"]][-self.HISTORY:][::-1],
                           "media_status": self.media_status.get(p["id"]), "progress": self.progress_for(p["id"]),
                           "their_progress": theirs.get(p["id"]),
                           "counts_at": (self.counts.get(p["id"]) or {}).get("at"),
                           "free_there": (self.counts.get(p["id"]) or {}).get("free_there"),
                           **({"items": self.items(p["id"]), "rows": self.rows(p["id"])} if live else {})} for p in peers],
                "media_kinds": list(MEDIA_KINDS) if self.media is not None else [],
                "seen": [{k: v for k, v in s.items() if k != "at"} for s in self.seen.values()
                         if s["id"] not in paired and now - s["at"] < SEEN_FOR_S]}

    # --- on the network: the beacon, and syncing now and then ---

    def heard(self, msg: dict, ip: str) -> None:
        """A beacon from another radio."""
        if not isinstance(msg, dict) or msg.get("sleepradio") != PROTOCOL or not isinstance(msg.get("id"), str) \
                or msg["id"] == self.id or not isinstance(msg.get("port"), int):
            return
        address = ip if msg["port"] == 80 else f"{ip}:{msg['port']}"
        if msg.get("ask") and time.time() - getattr(self, "_answered", 0) > 5:     # (a radio just started: answer now)
            self._answered = time.time()
            threading.Thread(target=self._send_beacon, daemon=True).start()
        was = self.seen.get(msg["id"])
        self.seen[msg["id"]] = {"id": msg["id"], "name": str(msg.get("name", ""))[:60], "address": address,
                                "version": str(msg.get("version", ""))[:20], "at": time.time()}
        peers = self.peers()
        mine = next((p for p in peers if p["id"] == msg["id"]), None)
        if mine is not None and mine.get("address") != address:      # (it moved: DHCP gave it another address)
            self._save_peers([{**p, "address": address} if p["id"] == msg["id"] else p for p in peers])
            log.info("sync: %s is at %s now", mine.get("name"), address)
        if mine is not None and was is None:                          # (back on the network: catch up)
            threading.Thread(target=self._quietly, args=(msg["id"],), daemon=True).start()

    def _quietly(self, peer_id: str) -> None:
        try:
            self.pull(peer_id)
        except Exception:
            log.exception("sync with %s failed", peer_id)

    def start(self) -> None:
        threading.Thread(target=self._beacon, name="sync-beacon", daemon=True).start()
        threading.Thread(target=self._listen, name="sync-listen", daemon=True).start()
        threading.Thread(target=self._every, name="sync", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _send_beacon(self, ask: bool = False) -> None:
        """Say this radio's here (ask: and ask the others to say so too -- just started, it wants to know now)."""
        try:
            if not self.protected():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(json.dumps({**self.hello(), **({"ask": True} if ask else {})}).encode(),
                             ("255.255.255.255", BEACON_PORT))
        except OSError:
            pass                                     # (no network: nothing to tell)

    def _beacon(self) -> None:
        self._stop.wait(1)                           # (the listener first, to hear the answers)
        self._send_beacon(ask=True)
        while not self._stop.wait(BEACON_EVERY_S):
            self._send_beacon()

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("", BEACON_PORT))
                    s.settimeout(5)
                    while not self._stop.is_set():
                        try:
                            data, (ip, _port) = s.recvfrom(2048)
                        except socket.timeout:
                            continue
                        try:
                            self.heard(json.loads(data), ip)
                        except ValueError:
                            pass
            except OSError:
                self._stop.wait(30)                  # (no network yet, or the port is taken: try again later)

    def _every(self) -> None:
        self._stop.wait(20 + random.uniform(0, 60))  # (let the radio settle first; and not in step with the others)
        while not self._stop.is_set():
            for p in self.peers():
                self._quietly(p["id"])
            self.next_at = time.time() + SYNC_EVERY_S
            self._stop.wait(SYNC_EVERY_S)


def _clean_measure(value, default: dict) -> dict:
    """A count another radio sent: {"n", "bytes"?} as numbers, else the default."""
    if isinstance(value, dict) and isinstance(value.get("n"), int) and value["n"] >= 0 \
            and (value.get("bytes") is None or isinstance(value.get("bytes"), int)):
        return {k: value[k] for k in ("n", "bytes") if k in value}
    return default


def _changes(name: str, before, after) -> dict:
    """{"added", "changed", "deleted"}: how after differs from before."""
    b, a = _as_map(name, before), _as_map(name, after)
    return {"added": sum(1 for k in a if k not in b),
            "changed": sum(1 for k in a if k in b and _canon(a[k]) != _canon(b[k])),
            "deleted": sum(1 for k in b if k not in a)}


def _gone(name: str, before, after) -> int:
    """How many of before's items aren't in after."""
    a = _as_map(name, after)
    return sum(1 for k in _as_map(name, before) if k not in a)


def _plain_of(collections: dict) -> dict:
    """All but the media (a library may change while it's being copied into; the rest of a sync goes on)."""
    return {n: v for n, v in collections.items() if not n.startswith("media:")}


def _digest(collections: dict) -> str:
    """The collections' contents, as a short fingerprint (a list's order doesn't count)."""
    return hashlib.sha256(_canon({n: _as_map(n, v) for n, v in collections.items()}).encode()).hexdigest()[:24]


def _clean_address(address: str) -> str:
    a = (address or "").strip().removeprefix("http://").removeprefix("https://").strip("/")
    if not a or "/" in a or " " in a or len(a) > 100:
        raise ValueError("an address like 192.168.1.20, or 192.168.1.20:8080")
    return a


def _get_voice(address: str, name: str):
    return urllib.request.urlopen(f"http://{address}/api/sync/voice?name={urllib.parse.quote(name)}", timeout=60)


def _get_file(address: str, kind: str, path: str):
    return urllib.request.urlopen(f"http://{address}/api/media/download?kind={kind}&path={urllib.parse.quote(path)}",
                                  timeout=30)


def _fetch(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    slow = "/api/sync/state" in url or "/api/sync/adopt" in url
    with urllib.request.urlopen(req, timeout=SLOW_TIMEOUT_S if slow else TIMEOUT_S) as r:
        return json.loads(r.read())


