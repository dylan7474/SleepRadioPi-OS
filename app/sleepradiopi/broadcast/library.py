"""Find the music and jingles on disk.

Plain paths replace SleepRadio's SAF folder pickers. Tags come from
mutagen; a file with no tags falls back to its file name (dropping a
leading "NN - " track number) and its artist folder, matching the
Artist/Album/NN - Title layout of the SleepRadioMusic library.

Reading the tags of a whole library takes about a minute on a Pi Zero 2 W,
so the results are cached (keyed by path, size and mtime) and only new or
changed files are read at startup.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import mutagen

from ..config.atomic import write_atomic

from .models import BroadcastTrack, JingleClip

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav"}
_TRACK_NUMBER = re.compile(r"^\d+\s*[-.]\s*")


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)


def _tags(path: Path):
    try:
        return mutagen.File(path, easy=True)
    except Exception:
        return None


def read_track(path: Path, root: Path) -> BroadcastTrack:
    tags = _tags(path)

    def tag(name: str) -> str:
        return ((tags.get(name) if tags else None) or [""])[0].strip()

    rel = path.relative_to(root)
    folder_artist = rel.parts[0] if len(rel.parts) > 2 else ""
    return BroadcastTrack(
        path=path,
        title=tag("title") or _TRACK_NUMBER.sub("", path.stem).replace("_", " "),
        artist=tag("artist") or tag("albumartist") or folder_artist,
        album=tag("album") or (rel.parts[-2] if len(rel.parts) > 1 else ""),
    )


TAG_CACHE_VERSION = 1


def _load_tag_cache(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("tracks", {}) if data.get("version") == TAG_CACHE_VERSION else {}


def scan_music(folder: Path, cache_path: Path | None = None) -> list[BroadcastTrack]:
    """All tracks under folder. With cache_path, tags are only read for files
    that are new or changed since the last scan (by size and mtime)."""
    old = _load_tag_cache(cache_path)
    new: dict[str, list] = {}
    tracks, read = [], 0
    for p in _audio_files(folder):
        st = p.stat()
        key = str(p)
        hit = old.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            track = BroadcastTrack(path=p, title=hit[2], artist=hit[3], album=hit[4])
        else:
            track = read_track(p, folder)
            read += 1
        new[key] = [st.st_size, st.st_mtime_ns, track.title, track.artist, track.album]
        tracks.append(track)
    if cache_path is not None and new != old:
        write_atomic(cache_path, json.dumps({"version": TAG_CACHE_VERSION, "tracks": new}))
    log.info("music: %d tracks in %s (tags read for %d, %d from the cache)",
             len(tracks), folder, read, len(tracks) - read)
    return tracks


def scan_jingles(folder: Path) -> list[JingleClip]:
    clips = []
    for p in _audio_files(folder):
        tags = _tags(p)
        length = getattr(getattr(tags, "info", None), "length", 0) or 0
        clips.append(JingleClip(p, float(length)))
    log.info("jingles: %d in %s", len(clips), folder)
    return clips
