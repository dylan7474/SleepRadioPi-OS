"""Broadcast data classes and config.

Port of SleepRadio's BroadcastModels.kt, with paths instead of SAF URIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class BroadcastTrack:
    path: Path
    title: str
    artist: str
    album: str = ""


@dataclass(frozen=True)
class JingleClip:
    path: Path
    duration_s: float


class LinkKind(Enum):
    """What the DJ says in the gap after a track (or nothing)."""

    NONE = "none"
    LINK = "link"
    IDENT = "ident"
    TIME_CHECK = "time_check"


class WindDownPhase(Enum):
    """NORMAL: full cadence. EASING: sparser, terser links. SILENT: music only."""

    NORMAL = "normal"
    EASING = "easing"
    SILENT = "silent"


class Chattiness(Enum):
    MAXIMUM = ("maximum", 1)  # a link before every track, no bare idents
    CHATTY = ("chatty", 2)
    BALANCED = ("balanced", 3)
    MINIMAL = ("minimal", 5)

    def __init__(self, ident: str, tracks_per_link: int) -> None:
        self.ident = ident
        self.tracks_per_link = tracks_per_link

    @classmethod
    def from_id(cls, s: str | None) -> "Chattiness":
        return next((c for c in cls if c.ident == s), cls.BALANCED)


@dataclass
class BroadcastConfig:
    tracks_per_link: int = Chattiness.BALANCED.tracks_per_link
    links_per_ident: int = 3
    links_per_time_check: int = 4
    # Maximum chattiness: never a bare ident, and time checks still name both tracks.
    announce_every_track: bool = False
    announcer_speed: float = 1.0
    news_speed: float = 0.75
    jingle_every: int = 0  # 0 = off
    dj_hooks_enabled: bool = False
    news_enabled: bool = False
    news_quiet_hours: bool = True
    news_quiet_start_min: int = 23 * 60
    news_quiet_end_min: int = 6 * 60
