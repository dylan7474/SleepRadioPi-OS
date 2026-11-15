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
             "broadcast_voice", "news_voice"}    # (the DJ's and the news's voice: each radio's choice; the packs themselves sync)
COLLECTION_KEYS = {"radio_stations", "receiver_names", "profiles", "playlists", "programmes", "messages",
                   "birthdays", "podcasts", "dj_hooks_own"}


def _synced_settings() -> list[str]:
    return sorted(f.name for f in backup_mod.SHARED if f.name not in RADIO_OWN | COLLECTION_KEYS)


def _settings_in(conf: dict) -> dict:
    defaults = asdict(Settings())
    out = {}
    for k in _synced_settings():
        try:
            out[k] = backup_mod._check(k, conf.get(k, defaults.get(k)))
        except ValueError:
            out[k] = defaults.get(k)
    return out


def _check_settings(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("settings: {...}")
    known = set(_synced_settings())
    return {k: backup_mod._check(k, v) for k, v in value.items() if k in known}     # (one this version hasn't: skipped)


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
    "settings": (_settings_in, _check_settings, None, lambda conf, v: dict(v)),
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
UNION_ONLY = {"voices", "wifi"}            # (only ever added: merged as a first sync is, every time)
WORDS = {**{f"files:{k}": k for k in FILE_KINDS}, "voices": "voices", "wifi": "Wi-Fi networks", "radio_stations": "stations", "receiver_names": "receiver names", "profiles": "themes", "playlists": "playlists",
         "programmes": "programmes", "messages": "messages", "message_times": "message times", "birthdays": "birthdays",
         "podcasts": "podcasts", "dj_hooks_own": "DJ lines", "settings": "settings"}


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
    if name == "settings":
        return _canon(value) == _canon(asdict(Settings()).get(key))
    return False


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
                got = max(mi, ti, key=_canon)
        elif mi is not _NONE and ti is not _NONE:
            if _canon(mi) == _canon(ti) or (bi is not _NONE and _canon(ti) == _canon(bi)):
                got = mi                                 # (the same, or only mine changed)
            elif bi is not _NONE and _canon(mi) == _canon(bi):
                got = ti                                 # only theirs changed
            else:
                got = max(mi, ti, key=_canon)            # both changed it (or both added it): the same rule on every radio
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
        conf = self._conf()
        self.id = conf.get("radio_id") if isinstance(conf.get("radio_id"), str) and conf.get("radio_id") else ""
        if not self.id:                          # (made once, kept: who this radio is to the others)
            self.id = uuid.uuid4().hex[:12]
            save_setting(self.config_file, "radio_id", self.id)

    def names(self) -> list[str]:
        """The collections this radio syncs: the files only if it has music storage."""
        return [n for n, c in COLLECTIONS.items() if c[0] is not None or (n == "wifi" and self.wifi_path is not None)
                or (n == "voices" and self.voice_jobs is not None) or (n.startswith("files:") and self.media is not None)]

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
        try:
            b = json.loads(self.base_file.read_text())
            return b if isinstance(b, dict) else {}
        except (OSError, ValueError):
            return {}

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

    def state(self, for_id: str = "", ip: str | None = None) -> dict:
        """This radio's collections; and (for_id: the radio asking) a fingerprint of the state this radio last
        agreed on with it -- if the two radios' fingerprints differ (a reply lost mid-sync, a re-pair, a card
        re-flashed), the next merge between them only adds: neither can tell a delete from a missed add."""
        conf = self._conf()
        base = self._bases().get(for_id) if for_id else None
        names = [n for n in self.names() if n != "wifi" or self._asked_by_peer(for_id, ip)]
        base = base if isinstance(base, dict) else {}
        return {**self.hello(), "collections": self._read(conf, names),
                "base": {n: _digest({n: v}) for n, v in base.items() if n not in UNION_ONLY}}

    def paired_with(self, peer: dict) -> None:
        """Another radio paired with this one (its id, name, address): keep it."""
        peers = [p for p in self.peers() if p["id"] != peer["id"]]
        self._save_peers(peers + [{"id": peer["id"], "name": peer.get("name", ""), "address": peer["address"]}])
        log.info("sync: paired with %s (%s)", peer.get("name"), peer["address"])

    def unpaired(self, peer_id: str) -> None:
        self._save_peers([p for p in self.peers() if p["id"] != peer_id])
        bases = self._bases()
        if bases.pop(peer_id, None) is not None:
            self._save_bases(bases)
        self.status.pop(peer_id, None)
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

    def allow_once(self, peer_id: str) -> None:
        self._allow_once.add(peer_id)

    def sync_now(self, peer_id: str) -> dict:
        return self.pull(peer_id)

    def pull(self, peer_id: str) -> dict:
        """One exchange with a paired radio: read its state, merge it with this radio's against the last
        state the two agreed on (the base), send it the result to take up (only if it hasn't changed
        meanwhile), then take it up here too: both end the same, and both keep it as the new base."""
        peer = next((p for p in self.peers() if p["id"] == peer_id), None)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        if self.protected():
            return self._said(peer_id, False, "this radio has a password: no sync")
        try:
            them = self.fetch(f"http://{peer['address']}/api/sync/state?for={self.id}")
        except (OSError, ValueError) as e:
            return self._said(peer_id, False, f"not answering ({str(getattr(e, 'reason', e))[:60]})")
        if not isinstance(them, dict) or them.get("id") != peer_id:
            return self._said(peer_id, False, "a different radio is at that address now")
        if them.get("sleepradio") != PROTOCOL:
            return self._said(peer_id, False, "it has a different version of the sync: update both radios")
        if them.get("protected"):
            return self._said(peer_id, False, "it has a password: no sync")
        try:
            names = [n for n in self.names() if n in them["collections"]]     # (what both sync)
            theirs = {n: COLLECTIONS[n][1](them["collections"][n]) for n in names}
        except (KeyError, TypeError, ValueError) as e:
            return self._said(peer_id, False, f"what it sent didn't check out ({e})")
        their_name = str(them.get("name") or "")
        with self._lock:
            kept = self._bases().get(peer_id)
            kept = kept if isinstance(kept, dict) else {}
            theirs_base = them.get("base") if isinstance(them.get("base"), dict) else {}
            # (each collection's base: only where both radios agree on it -- elsewhere (a reply lost mid-sync, a file
            # that didn't arrive, a re-pair) that one merges as a first sync does: adds only, never a delete)
            base = {n: kept[n] for n in names if n in kept and n not in UNION_ONLY
                    and theirs_base.get(n) == _digest({n: kept[n]})}
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
                    merged[n], here[n] = merge(n, base.get(n), mine[n], theirs[n])
                    there[n] = _gone(n, theirs[n], merged[n])                  # (what the other radio would lose)
                    for where, count, of in (("here", here[n]["deleted"], mine[n]), ("there", there[n], theirs[n])):
                        if count > HOLD_MIN and count > HOLD_DELETES * max(1, len(_as_map(n, of))) \
                                and peer_id not in self._allow_once:
                            held.append(f"{count} of {len(_as_map(n, of))} {WORDS[n]} {where}")
                    try:
                        merged[n] = check(merged[n])
                    except ValueError as e:                # (e.g. more stations together than a radio keeps)
                        return self._said(peer_id, False, f"together they don't fit: {e}")
            if held:
                return self._said(peer_id, False, "held: it would delete " + ", ".join(held) +
                                  ". Sync anyway to let it through", held=True)
            if not all(same(n, merged[n], theirs[n]) for n in names) \
                    or not all(n in base and same(n, base[n], merged[n]) for n in names if n not in UNION_ONLY):
                try:                                       # (the other radio takes it up, if it hasn't changed since)
                    ok = self.fetch(f"http://{peer['address']}/api/sync/adopt",
                                    {"id": self.id, "merged": merged, "was": _digest(theirs)})
                except (OSError, ValueError) as e:
                    return self._said(peer_id, False, f"it didn't take the sync ({str(getattr(e, 'reason', e))[:60]})")
                if not (isinstance(ok, dict) and ok.get("ok")):
                    return self._said(peer_id, False, "it changed meanwhile: it'll sync next time")
            with CONFIG_LOCK:
                conf = read_config(self.config_file)
                now = self._read(conf, names)
                if _digest(now) != _digest(mine):          # (changed here while it was asked: kept; the next sync merges it)
                    return self._said(peer_id, False, "changed here meanwhile: it'll sync next time")
                updates = self._write(conf, merged, mine)
            got = self._files(peer["address"], names, mine, merged)      # (the files: fetched / deleted here)
            self._keep_base(peer_id, {n: v for n, v in merged.items() if n in got or not n.startswith("files:")})
            self._allow_once.discard(peer_id)
        if their_name and their_name != peer.get("name"):
            self._save_peers([{**p, "name": their_name} if p["id"] == peer_id else p for p in self.peers()])
        words = lambda counts: ", ".join(f"{v} {k} {WORDS[n]}" for n, c in counts.items() for k, v in c.items() if v)
        here_w, there_w = words(here), words({n: _changes(n, theirs[n], merged[n]) for n in names})
        said = "; ".join(x for x in (here_w and f"here: {here_w}", there_w and f"on {their_name or 'it'}: {there_w}") if x)
        log.info("sync with %s: %s", their_name or peer_id, said or "already the same")
        return self._said(peer_id, True, said or "already the same")

    def adopt(self, peer_id: str, merged: dict, was: str, ip: str | None = None) -> bool:
        """Asked by a paired radio: take up the merged collections it worked out -- if this radio's are still
        what it read (was: their digest); else no (it syncs again later). Kept as the base with it too."""
        if not any(p["id"] == peer_id for p in self.peers()):
            raise ValueError("that radio isn't paired with this one")
        if not isinstance(merged, dict):
            raise ValueError("send the merged collections")
        names = [n for n in self.names() if n in merged and (n != "wifi" or self._asked_by_peer(peer_id, ip))]
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
                if set(mine) != set(names) or _digest(mine) != was:
                    return False
                updates = self._write(conf, new, mine)
            files = [n for n in names if (n.startswith("files:") or n == "voices") and not same(n, new[n], mine[n])]
            self._keep_base(peer_id, {n: v for n, v in new.items() if n not in files})     # (those once they're here)
            self.status[peer_id] = {"at": time.time(), "ok": True, "said": "synced (it asked)"}
        finally:
            self._lock.release()
        if files:                                    # (the files after, in the background: the other radio's waiting)
            peer = next(p for p in self.peers() if p["id"] == peer_id)
            threading.Thread(target=self._adopt_files, args=(peer_id, peer["address"], files, mine, new),
                             name="sync-files", daemon=True).start()
        return True

    def _adopt_files(self, peer_id, address, names, mine, new) -> None:
        with self._lock:
            got = self._files(address, names, mine, new)
            self._keep_base(peer_id, {**(self._bases().get(peer_id) or {}), **{n: new[n] for n in names if n in got}})

    def _files(self, address: str, names, mine: dict, want: dict) -> set:
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

    def _said(self, peer_id: str, ok: bool, said: str, held: bool = False) -> dict:
        st = {"at": time.time(), "ok": ok, "said": said, **({"held": True} if held else {})}
        self.status[peer_id] = st
        if not ok:
            log.info("sync: %s: %s", peer_id, said)
        return st

    def overview(self) -> dict:
        now = time.time()
        peers = self.peers()
        paired = {p["id"] for p in peers}
        return {"me": self.hello(),
                "peers": [{**p, "online": p["id"] in self.seen and now - self.seen[p["id"]]["at"] < SEEN_FOR_S,
                           "status": self.status.get(p["id"])} for p in peers],
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
            self._stop.wait(SYNC_EVERY_S)


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
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


