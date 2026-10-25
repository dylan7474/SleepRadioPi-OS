"""Track selection and DJ hook lines.

Port of SleepRadio's BroadcastSelector.kt and HookPool.kt -- pure logic,
deterministic with a seeded random.Random for tests.
"""

from __future__ import annotations

import math
import random
from collections import deque

from .models import BroadcastTrack

ARTIST_GAP = 5        # an artist isn't heard again within this many songs (less when a theme has few artists)
NO_REPEAT = 20        # nor a song within this many


def artist_key(artist: str) -> str:
    """"The Beatles", "Beatles", "beatles " -> "beatles"."""
    key = " ".join(artist.lower().split())
    return key[4:] if key.startswith("the ") and len(key) > 4 else key


class DeckStore:
    """Where each theme's shuffle is kept while the radio is on, so going back to
    a theme carries on where it was. Memory only: nothing is written to the card,
    and every power-on is a fresh shuffle."""

    def __init__(self) -> None:
        self.decks: dict = {}

    def get(self, key: str) -> dict | None:
        return self.decks.get(key)

    def put(self, key: str, state: dict) -> None:
        self.decks[key] = state


class BroadcastSelector:
    """Artist first, then a song. Each artist has their own shuffled bag: all their
    songs play once before any plays again. The next artist is picked at random
    but weighted by the square root of how many songs they have, so a big
    catalogue is heard more often without taking over, and never one heard in
    the last ARTIST_GAP songs. Artists are matched as artist_key() does, and a
    new bag doesn't open with a song just played. Songs added to the pool join
    their artist's bag; songs gone drop out. With a store (and a key: the
    theme), a theme carries on where it was when it's come back to."""

    def __init__(self, pool: list[BroadcastTrack], rng: random.Random | None = None,
                 store: DeckStore | None = None, key: str = "") -> None:
        self.pool = pool
        self.rng = rng or random.Random()
        self.store, self.key = store, key.lower()
        self._by_path = {str(t.path): t for t in pool}
        self._groups: dict[str, list[str]] = {}
        for p, t in self._by_path.items():
            self._groups.setdefault(artist_key(t.artist), []).append(p)
        self._weight = {a: math.sqrt(len(ps)) for a, ps in self._groups.items()}
        self._no_repeat = max(1, min(len(pool) // 2, NO_REPEAT))
        old = (store.get(self.key) if store is not None else None) or {}
        self._left: dict[str, list[str]] = {a: [p for p in old.get("left", {}).get(a, []) if p in self._by_path]
                                            for a in self._groups}
        self._done: dict[str, set[str]] = {a: {p for p in old.get("done", {}).get(a, ()) if p in self._by_path}
                                           for a in self._groups}
        self._recent_paths: deque = deque((p for p in old.get("recent", []) if p in self._by_path),
                                          maxlen=self._no_repeat)
        self._recent_artists: list[str] = [a for a in old.get("artists", []) if a in self._groups]
        for a, paths in self._groups.items():     # (songs not in the bag or the played ones: new, shuffled in)
            have = set(self._left[a]) | self._done[a]
            for p in paths:
                if p not in have:
                    self._left[a].insert(self.rng.randrange(len(self._left[a]) + 1), p)
        self._save()

    def has_tracks(self) -> bool:
        return bool(self.pool)

    def _save(self) -> None:
        if self.store is not None:
            self.store.put(self.key, {"left": self._left, "done": self._done,
                                      "recent": list(self._recent_paths), "artists": self._recent_artists})

    def _refill(self, artist: str) -> None:
        order = self._groups[artist][:]
        self.rng.shuffle(order)
        self._left[artist], self._done[artist] = order, set()

    def next_track(self) -> BroadcastTrack | None:
        if not self.pool:
            return None
        gap = min(ARTIST_GAP, len(self._groups) // 2)
        heard = self._recent_artists[-gap:] if gap else []
        artists = [a for a in self._groups if a not in heard]
        artist = self.rng.choices(artists, [self._weight[a] for a in artists])[0]
        if not self._left[artist]:
            self._refill(artist)
        bag = self._left[artist]
        i = next((i for i, p in enumerate(bag) if p not in self._recent_paths), 0)   # (not one just played)
        path = bag.pop(i)
        self._done[artist].add(path)
        self._recent_paths.append(path)
        if artist:                                # (songs with no artist tag aren't spaced out)
            self._recent_artists = (self._recent_artists + [artist])[-ARTIST_GAP:]
        self._save()
        return self._by_path[path]

    def put_back(self, track: BroadcastTrack) -> None:
        """A track that was lined up but won't play now (the theme changed): back
        on top of its artist's bag, so it still plays."""
        path, artist = str(track.path), artist_key(track.artist)
        if path in self._by_path and path in self._done.get(artist, ()):
            self._done[artist].discard(path)
            self._left[artist].insert(0, path)
            if path in self._recent_paths:        # (it never played)
                self._recent_paths.remove(path)
            if artist in self._recent_artists:
                self._recent_artists.reverse()
                self._recent_artists.remove(artist)
                self._recent_artists.reverse()
            self._save()


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
