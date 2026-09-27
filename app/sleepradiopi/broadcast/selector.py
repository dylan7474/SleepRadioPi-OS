"""Track selection and DJ hook lines.

Port of SleepRadio's BroadcastSelector.kt and HookPool.kt -- pure logic,
deterministic with a seeded random.Random for tests.
"""

from __future__ import annotations

import random
from collections import deque

from .models import BroadcastTrack

ARTIST_SPACING = 2


class BroadcastSelector:
    """Shuffle that won't replay a track within the last few picks, and spaces
    the same artist out by ARTIST_SPACING."""

    def __init__(self, pool: list[BroadcastTrack], rng: random.Random | None = None) -> None:
        self.pool = pool
        self.rng = rng or random.Random()
        self._no_repeat = max(1, min(len(pool) // 2, 20))
        self._recent_paths: deque = deque()
        self._recent_artists: deque = deque()

    def has_tracks(self) -> bool:
        return bool(self.pool)

    def next_track(self) -> BroadcastTrack | None:
        if not self.pool:
            return None
        candidates = (
            [t for t in self.pool
             if t.path not in self._recent_paths and t.artist.lower() not in self._recent_artists]
            or [t for t in self.pool if t.path not in self._recent_paths]
            or self.pool
        )
        pick = candidates[self.rng.randrange(len(candidates))]
        self._recent_paths.append(pick.path)
        while len(self._recent_paths) > self._no_repeat:
            self._recent_paths.popleft()
        if pick.artist.strip():
            self._recent_artists.append(pick.artist.lower())
            while len(self._recent_artists) > ARTIST_SPACING:
                self._recent_artists.popleft()
        return pick


class HookPool:
    """Shuffle-bag of hook lines: each is used once before any repeats, and a
    fresh shuffle never opens with the one just spoken."""

    def __init__(self, hooks: list[str], rng: random.Random | None = None) -> None:
        self.all = list(dict.fromkeys(h.strip() for h in hooks if h.strip()))
        self.rng = rng or random.Random()
        self._bag: deque = deque()
        self._last: str | None = None

    def __len__(self) -> int:
        return len(self.all)

    def next(self) -> str | None:
        if not self.all:
            return None
        if not self._bag:
            shuffled = self.all[:]
            self.rng.shuffle(shuffled)
            self._bag.extend(shuffled)
            if len(self.all) > 1 and self._bag[0] == self._last:
                self._bag.rotate(-1)
        self._last = self._bag.popleft()
        return self._last


def parse_hooks(text: str) -> list[str]:
    """One hook per line; blank lines and '#' comments are skipped."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]
