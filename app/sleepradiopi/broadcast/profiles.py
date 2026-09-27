"""Profiles: named lists of artists, e.g. a "Friday List", to play only them.

Like artist radio, but for several artists: the station plays only tracks by
the artists on the list, and the DJ says the list's name ("welcome to Friday
List on Sleep Radio"; a name ending in "Radio" is used as it is). Kept in
the config as "profiles" -- [{"name": "Friday List", "artists": [...]}] --
with the one playing as "broadcast_profile".
"""

from __future__ import annotations

from .script_builder import DEFAULT_STATION

MAX_PROFILES = 50
MAX_ARTISTS = 500
NAME_MAX = 40


def validate(profiles) -> list[dict]:
    """Check a list from the page or a settings file; returns it cleaned up.
    ValueError says what's wrong."""
    if not isinstance(profiles, list):
        raise ValueError("profiles must be a list")
    if len(profiles) > MAX_PROFILES:
        raise ValueError(f"at most {MAX_PROFILES} lists")
    out, seen = [], set()
    for p in profiles:
        if not isinstance(p, dict):
            raise ValueError("each list needs a name and artists")
        name = p.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
            raise ValueError(f"a list's name must be 1-{NAME_MAX} characters")
        name = " ".join(name.split())
        if name.lower() in seen:
            raise ValueError(f"there are two lists called {name}")
        seen.add(name.lower())
        artists = p.get("artists")
        if not isinstance(artists, list) or not artists:
            raise ValueError(f"{name}: pick at least one artist")
        if len(artists) > MAX_ARTISTS or not all(isinstance(a, str) and a.strip() and len(a) <= 200
                                                 for a in artists):
            raise ValueError(f"{name}: the artists must be names")
        out.append({"name": name, "artists": sorted({" ".join(a.split()) for a in artists}, key=str.lower)})
    return out


def station_name(profile_name: str) -> str:
    """"Friday List" -> "Friday List on Sleep Radio"; "Rock Radio" stays."""
    if profile_name.lower().endswith("radio"):
        return profile_name
    return f"{profile_name} on {DEFAULT_STATION}"
