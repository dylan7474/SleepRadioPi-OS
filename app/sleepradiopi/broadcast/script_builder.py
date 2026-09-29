"""When the DJ speaks (ShowClock) and what it says (DjScriptBuilder).

Port of SleepRadio's DjScriptBuilder.kt: pure text templating, plain text
(no SSML) because the Piper voice reads it as-is. Deterministic with a
seeded random.Random.
"""

from __future__ import annotations

import random
from datetime import datetime
from datetime import time as Time

from .models import BroadcastConfig, BroadcastTrack, LinkKind, WindDownPhase
from .selector import HookPool
from .speech import normalize_for_speech


class ShowClock:
    """Call on_track_started() once as each track begins; it returns the link
    to play in the gap *after* that track. EASING doubles the gap and drops
    idents/time checks; SILENT stops links. 06:00-10:59 has time checks twice
    as often."""

    def __init__(self, config: BroadcastConfig) -> None:
        self.config = config
        self._tracks_since_link = 0
        self._link_count = 0

    def on_track_started(self, now: Time | None = None,
                         phase: WindDownPhase = WindDownPhase.NORMAL) -> LinkKind:
        now = now or datetime.now().time()
        if phase == WindDownPhase.SILENT:
            self._tracks_since_link = 0
            return LinkKind.NONE
        self._tracks_since_link += 1
        gap = self.config.tracks_per_link * (2 if phase == WindDownPhase.EASING else 1)
        if self._tracks_since_link < gap:
            return LinkKind.NONE
        self._tracks_since_link = 0
        self._link_count += 1
        if phase == WindDownPhase.EASING:
            return LinkKind.LINK
        every = self.config.links_per_time_check
        if 6 <= now.hour <= 10:
            every = max(every // 2, 2)
        if not self.config.announce_every_track and self._link_count % self.config.links_per_ident == 0:
            return LinkKind.IDENT
        if self._link_count % every == 0:
            return LinkKind.TIME_CHECK
        return LinkKind.LINK

    def reset(self) -> None:
        self._tracks_since_link = 0
        self._link_count = 0


WELCOME_TAILS = ["Let's begin.", "Here's the music.", "Settle in.", "Let's get started."]
OPENERS = ["First up,", "We begin with", "Kicking off with", "To start,"]
IDENTS = [                   # {station}: "Sleep Radio", or e.g. "Beatles Radio"
    "You're listening to {station}.",
    "This is {station} — music through the night.",
    "{station}. Stay with us.",
]
OUTROS = ["That was", "You just heard", "We just heard"]
INTROS = ["Coming up,", "Next up,", "Here's", "Let's hear"]
EXACT_TIME_LEADS = ["The time is", "It's"]
STATION_ONLY = ["You're with {station}.", "More music in a moment."]
DEFAULT_STATION = "Sleep Radio"      # (the box radio's name; the radio's own is config/brand.name)


def artist_station_name(artist: str | None) -> str:
    """"The Beatles" -> "Beatles Radio"; None -> the radio's name ("Sleep Radio", "Phonosphere")."""
    if not artist:
        from sleepradiopi.config import brand
        return brand.name
    name = artist.strip()
    if name.lower().startswith("the ") and len(name) > 4:
        name = name[4:]
    return f"{name} Radio"


class DjScriptBuilder:
    def __init__(self, rng: random.Random | None = None, hooks: HookPool | None = None,
                 station: str | None = None) -> None:
        from sleepradiopi.config import brand
        self.rng = rng or random.Random()
        self.hooks = hooks
        self.station = station or brand.name   # the name the DJ uses; changes with artist radio

    def _pick(self, options: list[str]) -> str:
        return options[self.rng.randrange(len(options))].format(station=self.station)

    def welcome(self, first: BroadcastTrack | None = None, now: Time | None = None) -> str:
        if first is not None:
            return f"{self.welcome_greeting(now)} {self.welcome_first_track(first)}"
        return f"{self.welcome_greeting(now)} {self._pick(WELCOME_TAILS)}"

    def welcome_greeting(self, now: Time | None = None, time_known: bool = True) -> str:
        if not time_known:   # offline with no clock: don't guess the time of day
            return f"Hello, and welcome to {self.station}."
        hour = (now or datetime.now().time()).hour
        greeting = ("Good morning" if 5 <= hour <= 11 else
                    "Good afternoon" if 12 <= hour <= 17 else
                    "Good evening" if 18 <= hour <= 21 else "Hello")
        return f"{greeting}, and welcome to {self.station}."

    def welcome_first_track(self, first: BroadcastTrack) -> str:
        return f"{self._pick(OPENERS)} {track_phrase(first)}."

    def build(self, kind: LinkKind, previous: BroadcastTrack | None, next_: BroadcastTrack | None,
              now: Time | None = None, terse: bool = False,
              announce_every_track: bool = False) -> str:
        if kind == LinkKind.NONE:
            return ""
        if kind == LinkKind.IDENT:
            return self._pick(IDENTS)
        if kind == LinkKind.TIME_CHECK:
            lead = self.time_line(now)
            if terse:
                return lead
            outro = (f"{self._pick(OUTROS)} {track_phrase(previous)}. "
                     if announce_every_track and previous else "")
            intro = f" Here's {track_phrase(next_)}." if next_ else ""
            return f"{outro}{lead}{intro}"
        if terse:
            return self.outro_line(previous)
        hook = self.hooks.next() if self.hooks else None
        return f"{hook or self.outro_line(previous)} {self.intro_line(next_)}"

    def outro_line(self, previous: BroadcastTrack | None) -> str:
        if previous is None:
            return self._pick(STATION_ONLY)
        return f"{self._pick(OUTROS)} {track_phrase(previous)}."

    def intro_line(self, next_: BroadcastTrack | None) -> str:
        if next_ is None:
            return self._pick(STATION_ONLY)
        return f"{self._pick(INTROS)} {track_phrase(next_)}."

    def album_intro(self, album: dict, first: BroadcastTrack) -> str:
        """Before an album played start to finish ({"title", "artist"})."""
        lead = ALBUM_INTROS[self.rng.randrange(len(ALBUM_INTROS))]
        return lead.format(album=album_phrase(album), first=normalize_for_speech(first.title.strip()) or "track one")

    def album_outro(self, album: dict) -> str:
        return ALBUM_OUTROS[self.rng.randrange(len(ALBUM_OUTROS))].format(album=album_phrase(album))

    def time_line(self, now: Time | None = None) -> str:
        now = now or datetime.now().time()
        return f"{self._time_lead(now)} {spoken_time(now)}."

    def _time_lead(self, now: Time) -> str:
        off = minutes_past_rounded(now)
        if off < 0:
            return "It's coming up to"
        if off > 0:
            return "It's just gone"
        return self._pick(EXACT_TIME_LEADS)


ALBUM_INTROS = [
    "Now, an album from start to finish: {album}. It opens with {first}.",
    "Time for a whole album: {album}, from the top. Here's {first}.",
    "Let's hear an album all the way through: {album}. First, {first}.",
]
ALBUM_OUTROS = [
    "That was {album}, from start to finish.",
    "And that was the whole of {album}.",
]


def album_phrase(album: dict) -> str:
    """'Rubber Soul, by The Beatles' (or 'by various artists')."""
    title = normalize_for_speech(album["title"].strip()) or "that album"
    artist = normalize_for_speech(album["artist"].strip())
    return f"{title}, by {artist.lower() if artist == 'Various artists' else artist}" if artist else title


def track_phrase(t: BroadcastTrack) -> str:
    """'Song, by Artist', or just the title when the artist is unknown/blank/==title."""
    title = normalize_for_speech(t.title.strip()) or "that one"
    artist = normalize_for_speech(t.artist.strip())
    if not artist or artist.lower() in ("unknown", title.lower()):
        return title
    return f"{title}, by {artist}"


def minutes_past_rounded(t: Time) -> int:
    """Minutes t is past (+) or short of (-) the five-minute mark spoken_time rounds to: -2..+2."""
    return t.minute - ((t.minute + 2) // 5) * 5


_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 20: "twenty", 25: "twenty-five",
}


def spoken_time(t: Time) -> str:
    """A loose, TTS-friendly spoken time, rounded to five minutes -- no digits."""
    mins = ((t.minute + 2) // 5) * 5
    hour = t.hour
    if mins == 60:
        mins, hour = 0, (hour + 1) % 24
    h12 = (hour + 11) % 12 + 1
    hour_word = _NUMBER_WORDS[h12]
    next_hour_word = _NUMBER_WORDS[h12 % 12 + 1]
    if mins == 0:
        return {0: "midnight", 12: "midday"}.get(hour, f"{hour_word} o'clock")
    if mins == 15:
        return f"quarter past {hour_word}"
    if mins == 30:
        return f"half past {hour_word}"
    if mins == 45:
        return f"quarter to {next_hour_word}"
    if mins < 30:
        return f"{_NUMBER_WORDS[mins]} minutes past {hour_word}"
    return f"{_NUMBER_WORDS[60 - mins]} minutes to {next_hour_word}"
