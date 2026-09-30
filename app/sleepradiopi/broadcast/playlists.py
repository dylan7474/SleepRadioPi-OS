"""Playlists: your own lists of tracks, played in order or shuffled.

Kept in the config as "playlists" -- [{"name": "Sunday", "tracks": [["music",
"The Beatles/Rubber Soul/02 - Michelle.mp3"], ["ondemand", "Storms/rain.mp3"]]}]
-- each track as its library ("music" or "ondemand") and its path inside it,
so a playlist survives the library being rescanned; a file that's gone is
skipped. Played through the show's queue, like requests, so the DJ talks
between them as usual when it's on, and not at all when it's off.
"""

from __future__ import annotations

MAX_PLAYLISTS = 50
MAX_TRACKS = 1000
NAME_MAX = 40
ROOTS = ("music", "ondemand")


def _clean_track(t) -> list[str]:
    if not (isinstance(t, (list, tuple)) and len(t) == 2 and t[0] in ROOTS and isinstance(t[1], str)):
        raise ValueError("each track is [\"music\" or \"ondemand\", its path]")
    path = t[1].strip().strip("/")
    if not path or len(path) > 1000 or ".." in path.split("/"):
        raise ValueError("a track's path isn't in the library")
    return [t[0], path]


def validate(playlists) -> list[dict]:
    """Check a list from the page or a settings file; returns it cleaned up.
    ValueError says what's wrong."""
    if not isinstance(playlists, list):
        raise ValueError("playlists must be a list")
    if len(playlists) > MAX_PLAYLISTS:
        raise ValueError(f"at most {MAX_PLAYLISTS} playlists")
    out, seen = [], set()
    for p in playlists:
        if not isinstance(p, dict):
            raise ValueError("each playlist needs a name and tracks")
        name = p.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
            raise ValueError(f"a playlist's name must be 1-{NAME_MAX} characters")
        name = " ".join(name.split())
        if name.lower() in seen:
            raise ValueError(f"there are two playlists called {name}")
        seen.add(name.lower())
        tracks = p.get("tracks", [])
        if not isinstance(tracks, list) or len(tracks) > MAX_TRACKS:
            raise ValueError(f"{name}: up to {MAX_TRACKS} tracks")
        out.append({"name": name, "tracks": [_clean_track(t) for t in tracks]})
    return out


def find(playlists: list[dict], name) -> dict | None:
    if not isinstance(name, str):
        return None
    key = " ".join(name.split()).lower()
    return next((p for p in playlists if p["name"].lower() == key), None)
