"""Cases ported from SleepRadio's BroadcastTest.kt / HookPoolTest.kt, so the
Python port is held to the same expectations as the Android app."""

import random
from datetime import time as T
from pathlib import Path

import pytest

from sleepradiopi.broadcast.models import (
    BroadcastConfig, BroadcastTrack, Chattiness, LinkKind, WindDownPhase,
)
from sleepradiopi.broadcast.script_builder import (
    DjScriptBuilder, ShowClock, minutes_past_rounded, spoken_time, track_phrase,
)
from sleepradiopi.broadcast.selector import BroadcastSelector, HookPool, parse_hooks
from sleepradiopi.broadcast.speech import normalize_for_speech


def track(title: str, artist: str) -> BroadcastTrack:
    return BroadcastTrack(Path(f"/m/{artist}/{title}.mp3"), title, artist)


@pytest.mark.parametrize("t, words", [
    (T(0, 2), "midnight"),
    (T(12, 1), "midday"),
    (T(9, 0), "nine o'clock"),
    (T(15, 14), "quarter past three"),
    (T(22, 31), "half past ten"),
    (T(19, 44), "quarter to eight"),
    (T(18, 9), "ten minutes past six"),
    (T(16, 41), "twenty minutes to five"),
    (T(23, 58), "midnight"),
])
def test_spoken_time(t, words):
    assert spoken_time(t) == words


def test_time_line_lead_matches_side_of_the_mark():
    b = DjScriptBuilder(random.Random(1))
    assert b.time_line(T(19, 28)).startswith("It's coming up to half past")
    assert b.time_line(T(19, 32)).startswith("It's just gone half past")
    assert b.time_line(T(19, 59)).startswith("It's coming up to eight")
    assert minutes_past_rounded(T(10, 30)) == 0


@pytest.mark.parametrize("raw, spoken", [
    ("1984", "nineteen eighty-four"),
    ("1900", "nineteen hundred"),
    ("1901", "nineteen oh one"),
    ("2000", "two thousand"),
    ("2005", "two thousand and five"),
    ("2024", "twenty twenty-four"),
    ("Highway 61", "Highway sixty-one"),
    ("0123", "one hundred and twenty-three"),
    ("Track 21st", "Track twenty-first"),
    ("100th", "one hundredth"),
    ("Sum41", "Sum forty-one"),
    ("2Pac", "two Pac"),
    ("H2O", "H two O"),
    ("Blink182", "Blink one hundred and eighty-two"),
    ("ub40", "U B forty"),
    ("Food for Thought, by UB40", "Food for Thought, by U B forty"),
    ("10cc", "ten C C"),
    ("Hits of the 70s", "Hits of the seventies"),
    ("1990s", "nineteen nineties"),
    ("2000s", "two thousands"),
    ("", ""),
    ("The Beatles", "The Beatles"),
])
def test_normalize_for_speech(raw, spoken):
    assert normalize_for_speech(raw) == spoken


def test_welcome_greeting_by_time_of_day():
    b = DjScriptBuilder(random.Random(1))
    assert b.welcome(now=T(8, 0)).startswith("Good morning")
    assert b.welcome(now=T(14, 0)).startswith("Good afternoon")
    assert b.welcome(now=T(20, 0)).startswith("Good evening")
    assert b.welcome(now=T(2, 0)).startswith("Hello")
    assert "welcome to Sleep Radio" in b.welcome(track("Pink Moon", "Nick Drake"), T(20, 0))


def test_track_phrase_hides_unknown_or_repeated_artist():
    assert track_phrase(track("Pink Moon", "Nick Drake")) == "Pink Moon, by Nick Drake"
    assert track_phrase(track("Pink Moon", "Unknown")) == "Pink Moon"
    assert track_phrase(track("Queen", "Queen")) == "Queen"


def test_link_names_both_tracks_and_hooks_replace_the_outro():
    prev, nxt = track("Pink Moon", "Nick Drake"), track("Money", "Pink Floyd")
    plain = DjScriptBuilder(random.Random(3)).build(LinkKind.LINK, prev, nxt)
    assert "Pink Moon, by Nick Drake" in plain and "Money, by Pink Floyd" in plain
    hooked = DjScriptBuilder(random.Random(3), HookPool(["Smooth sounds tonight."])).build(
        LinkKind.LINK, prev, nxt)
    assert hooked.startswith("Smooth sounds tonight.") and "Money, by Pink Floyd" in hooked


def test_show_clock_cadence_balanced():
    clock = ShowClock(BroadcastConfig(tracks_per_link=Chattiness.BALANCED.tracks_per_link))
    kinds = [clock.on_track_started(T(20, 0)) for _ in range(12)]
    links = [k for k in kinds if k != LinkKind.NONE]
    assert kinds[:3] == [LinkKind.NONE, LinkKind.NONE, LinkKind.LINK]
    assert links == [LinkKind.LINK, LinkKind.LINK, LinkKind.IDENT, LinkKind.TIME_CHECK]


def test_show_clock_maximum_never_idents():
    clock = ShowClock(BroadcastConfig(tracks_per_link=1, announce_every_track=True))
    kinds = [clock.on_track_started(T(20, 0)) for _ in range(12)]
    assert LinkKind.IDENT not in kinds and LinkKind.NONE not in kinds
    assert kinds.count(LinkKind.TIME_CHECK) == 3


def test_show_clock_silent_wind_down_stops_links():
    clock = ShowClock(BroadcastConfig(tracks_per_link=1))
    assert clock.on_track_started(T(20, 0), WindDownPhase.SILENT) == LinkKind.NONE


def test_selector_spaces_artists_and_avoids_repeats():
    pool = [track(f"Song {i}", f"Artist {i % 4}") for i in range(12)]
    sel = BroadcastSelector(pool, random.Random(7))
    picks = [sel.next_track() for _ in range(60)]
    for a, b, c in zip(picks, picks[1:], picks[2:]):
        assert len({a.artist, b.artist, c.artist}) == 3
    for i in range(len(picks) - 6):
        assert len({p.path for p in picks[i:i + 6]}) == 6


def test_selector_empty_pool():
    assert BroadcastSelector([]).next_track() is None


def test_hook_pool_uses_every_hook_before_repeating():
    pool = HookPool([f"hook {i}" for i in range(10)], random.Random(2))
    first = [pool.next() for _ in range(10)]
    assert sorted(first) == sorted(f"hook {i}" for i in range(10))
    second_start = pool.next()
    assert second_start != first[-1]


def test_parse_hooks_skips_blanks_and_comments():
    assert parse_hooks("# header\n\nOne.\n  Two.  \n#x\n") == ["One.", "Two."]
