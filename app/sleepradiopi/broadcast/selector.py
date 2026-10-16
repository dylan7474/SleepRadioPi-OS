"""Track selection and DJ hook lines.

Port of SleepRadio's BroadcastSelector.kt and HookPool.kt -- pure logic,
deterministic with a seeded random.Random for tests.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import deque
from pathlib import Path

from .models import BroadcastTrack

log = logging.getLogger(__name__)

ARTIST_SPACING = 2
DECK_SAVE_S = 900     # a deck's place is written at most this often (the card's sake): a power cut
                      # can only mean the last few tracks come round again in that deck


class DeckStore:
    """Where each theme's deck is kept between starts (a JSON file; None: only
    in memory). A lost or broken file just means a fresh shuffle."""

    def __init__(self, path: Path | None = None, save_s: float = DECK_SAVE_S) -> None:
        self.path = Path(path) if path is not None else None
        self.save_s = save_s
        self._saved = -1e9                       # time.monotonic() of the last write
        self._lock = threading.Lock()
        self.decks: dict = {}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self.decks = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                pass

    def get(self, key: str) -> dict:
        d = self.decks.get(key) or {}
        return {name: [p for p in d.get(name, []) if isinstance(p, str)] for name in ("left", "done", "recent")}

    def put(self, key: str, deck: dict) -> None:
        with self._lock:
            self.decks[key] = deck
            if self.path is None or time.monotonic() - self._saved < self.save_s:
                return
            self._saved = time.monotonic()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.decks))
                os.replace(tmp, self.path)
            except OSError as e:
                log.warning("deck: can't save %s: %s", self.path, e)


class BroadcastSelector:
    """A shuffled deck: every track in the pool plays once, in a random order,
    before any plays again. The same artist is spaced out by ARTIST_SPACING
    (the next track down the deck by another artist goes first; near the end
    of a deck, with only those artists left, less), and a new
    deck doesn't open with the last few played. Tracks added to the pool
    join the deck being played; tracks gone from it drop out. With a store
    (and a key: the theme), the deck carries on where it was after a restart."""

    def __init__(self, pool: list[BroadcastTrack], rng: random.Random | None = None,
                 store: DeckStore | None = None, key: str = "") -> None:
        self.pool = pool
        self.rng = rng or random.Random()
        self.store, self.key = store, key.lower()
        self._no_repeat = max(1, min(len(pool) // 2, 20))
        self._by_path = {str(t.path): t for t in pool}
        d = store.get(self.key) if store is not None else {"left": [], "done": [], "recent": []}
        self._left: list[str] = [p for p in d["left"] if p in self._by_path]
        self._done: set[str] = {p for p in d["done"] if p in self._by_path}
        self._recent_paths: deque = deque((p for p in d["recent"] if p in self._by_path), maxlen=self._no_repeat)
        self._recent_artists: deque = deque(
            (self._by_path[p].artist.lower() for p in list(self._recent_paths)[-ARTIST_SPACING:]
             if self._by_path[p].artist.strip()), maxlen=ARTIST_SPACING)
        self._add_new()

    def has_tracks(self) -> bool:
        return bool(self.pool)

    def _add_new(self) -> None:
        """Tracks in the pool that aren't in this deck yet: shuffled in among those left."""
        have = set(self._left) | self._done
        for p in self._by_path:
            if p not in have:
                self._left.insert(self.rng.randrange(len(self._left) + 1), p)

    def _new_deck(self) -> None:
        order = list(self._by_path)
        self.rng.shuffle(order)
        recent = set(self._recent_paths)        # (not straight back to what's just played)
        self._left = [p for p in order if p not in recent] + [p for p in order if p in recent]
        self._done = set()

    def next_track(self) -> BroadcastTrack | None:
        if not self.pool:
            return None
        if not self._left:
            self._new_deck()
        recent = set(self._recent_paths)
        ok = [i for i, p in enumerate(self._left) if p not in recent] or [0]
        artists = list(self._recent_artists)
        # The next one down the deck by an artist not heard in the last ARTIST_SPACING;
        # near a deck's end, when only those are left, not the last one's at least,
        # then simply the next (a deck never plays a track twice).
        i = next((i for i in ok if self._by_path[self._left[i]].artist.lower() not in artists), None)
        if i is None and artists:
            i = next((i for i in ok if self._by_path[self._left[i]].artist.lower() != artists[-1]), None)
        path = self._left.pop(ok[0] if i is None else i)
        self._done.add(path)
        pick = self._by_path[path]
        self._recent_paths.append(path)
        if pick.artist.strip():
            self._recent_artists.append(pick.artist.lower())
        self._save()
        return pick

    def put_back(self, track: BroadcastTrack) -> None:
        """A track that was lined up but won't play now (the theme changed): back
        on top of the deck, so this deck still plays it."""
        path = str(track.path)
        if path in self._by_path and path in self._done:
            self._done.discard(path)
            self._left.insert(0, path)
            if path in self._recent_paths:      # (it never played)
                self._recent_paths.remove(path)
            self._save()

    def _save(self) -> None:
        if self.store is not None:
            self.store.put(self.key, {"left": self._left, "done": sorted(self._done),
                                      "recent": list(self._recent_paths)})


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
