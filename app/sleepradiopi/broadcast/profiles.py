"""Profiles: named lists of artists, e.g. a "Friday List", to play only them.

Like artist radio, but for several artists: the station plays only tracks by
the artists on the list, and the DJ names it as a station ("welcome to Friday
List Radio"; a name already ending in "Radio" is used as it is). Kept in
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
        if "/" in name or "\\" in name or name.startswith("."):    # (it names the theme's jingles folder)
            raise ValueError(f"a list's name can't have / or \\ in it, or start with a dot: {name}")
        if name.lower() in seen:
            raise ValueError(f"there are two lists called {name}")
        seen.add(name.lower())
        everything = p.get("all") is True               # all my music (the main show is one of these)
        artists = p.get("artists") or ([] if everything else None)
        if not isinstance(artists, list) or (not artists and not everything):
            raise ValueError(f"{name}: pick at least one artist")
        if len(artists) > MAX_ARTISTS or not all(isinstance(a, str) and a.strip() and len(a) <= 200
                                                 for a in artists):
            raise ValueError(f"{name}: the artists must be names")
        entry = {"name": name, "artists": sorted({" ".join(a.split()) for a in artists}, key=str.lower)}
        if everything:
            entry["all"] = True
        own = validate_settings(p.get("settings"), name)
        if own:
            entry["settings"] = own
        out.append(entry)
    return out


# What a theme can set for itself; anything it doesn't set is the radio's (cascading: the
# radio-wide value, then the theme's own). DJ on/off isn't here: it's a live switch for the
# whole radio (buttons and programmes flip it).
CHATTINESS = ("maximum", "chatty", "balanced", "minimal")
THEME_SETTINGS = ("chattiness", "time_checks", "news", "jingle_every", "dj_hooks")


def validate_settings(own, name: str = "") -> dict:
    """A theme's own settings, cleaned up: {} (or None) is everything as the radio."""
    if own in (None, {}):
        return {}
    if not isinstance(own, dict) or any(k not in THEME_SETTINGS for k in own):
        raise ValueError(f"{name}: a theme can set {', '.join(THEME_SETTINGS)}")
    out = {}
    for k, v in own.items():
        if v is None:                                    # (None: as the radio)
            continue
        if k == "chattiness" and v not in CHATTINESS:
            raise ValueError(f"{name}: chattiness is one of {', '.join(CHATTINESS)}")
        if k in ("time_checks", "news", "dj_hooks") and not isinstance(v, bool):
            raise ValueError(f"{name}: {k} is true or false")
        if k == "jingle_every" and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 50):
            raise ValueError(f"{name}: jingles are 0 (off) to every 50 songs")
        out[k] = v
    return out


def station_name(profile_name: str) -> str:
    """A theme is a station: "Carisbrooke" -> "Carisbrooke Radio"; "Rock Radio" stays."""
    name = profile_name.strip()
    return name if name.lower().endswith("radio") else f"{name} Radio"
