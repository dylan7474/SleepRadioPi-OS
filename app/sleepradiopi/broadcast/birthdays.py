"""Birthday wishes: on someone's birthday the DJ wishes them a happy one.

The list starts empty and is filled in on the web page (it's kept in the
config as "birthdays", so it's in the saved settings file too). Each entry
is {"name": "Sarah", "day": 14, "month": 3} with an optional "year" for
"happy fortieth birthday". A 29 February birthday is celebrated on the 28th
in other years.

On the day, a wish goes at the start of a gap between songs at most every
WISH_EVERY_S and MAX_WISHES a day -- only when the clock can be trusted
(offline with no clock the date may be wrong) and not in the news quiet
hours, so nobody's woken at 3 am by a birthday cheer.
"""

from __future__ import annotations

import calendar
import random
from datetime import date, datetime, time as Time

WISH_EVERY_S = 90 * 60
MAX_WISHES = 4
MAX_PEOPLE = 200
NAME_MAX = 60

_ONES = ("", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
         "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
         "seventeenth", "eighteenth", "nineteenth")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_TENS_TH = ("", "", "twentieth", "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth",
            "eightieth", "ninetieth")


def ordinal_words(n: int) -> str:
    """1 -> "first", 40 -> "fortieth", 21 -> "twenty-first", 100 -> "hundredth"."""
    if not 1 <= n <= 120:
        raise ValueError(n)
    if n >= 100:
        rest = n - 100
        return "hundredth" if rest == 0 else f"hundred and {ordinal_words(rest)}"
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS_TH[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"


def validate(entries) -> list[dict]:
    """Check a list from the page or a settings file; returns it cleaned up
    (names trimmed, sorted by date). ValueError says what's wrong."""
    if not isinstance(entries, list):
        raise ValueError("birthdays must be a list")
    if len(entries) > MAX_PEOPLE:
        raise ValueError(f"at most {MAX_PEOPLE} birthdays")
    out = []
    this_year = date.today().year
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("each birthday needs a name, day and month")
        name = e.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
            raise ValueError(f"a name must be 1-{NAME_MAX} characters")
        day, month, year = e.get("day"), e.get("month"), e.get("year")
        for v in (day, month):
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"{name.strip()}: the day and month must be numbers")
        if not 1 <= month <= 12 or not 1 <= day <= (29 if month == 2 else calendar.monthrange(2001, month)[1]):
            raise ValueError(f"{name.strip()}: {day}/{month} isn't a date")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)
                                 or not 1900 <= year <= this_year
                                 or (month == 2 and day == 29 and not calendar.isleap(year))):
            raise ValueError(f"{name.strip()}: the year must be 1900-{this_year} (or left out)")
        entry = {"name": " ".join(name.split()), "day": day, "month": month}
        if year is not None:
            entry["year"] = year
        out.append(entry)
    return sorted(out, key=lambda e: (e["month"], e["day"], e["name"].lower()))


def todays(entries: list[dict], today: date) -> list[dict]:
    """Whose birthday it is today (with "age" if their year is known)."""
    found = []
    for e in entries:
        day, month = e["day"], e["month"]
        if month == 2 and day == 29 and not calendar.isleap(today.year):
            day = 28
        if (month, day) == (today.month, today.day):
            age = today.year - e["year"] if e.get("year") else None
            found.append({"name": e["name"], "age": age if age and 1 <= age <= 120 else None})
    return found


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def wish_text(people: list[dict], station: str, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    phrases = [f"happy {ordinal_words(p['age'])} birthday to {p['name']}" if p.get("age")
               else f"happy birthday to {p['name']}" for p in people]
    wish = _join(phrases)
    templates = [
        "A very {wish}! Many happy returns from everyone at {station}.",
        "Before the next song, a special message: {wish}. Have a wonderful day!",
        "It's a birthday today here on {station}, so: {wish}!",
        "We've a birthday to celebrate: {wish}. Hope it's a lovely one.",
    ]
    text = rng.choice(templates).format(wish=wish, station=station)
    return text[0].upper() + text[1:]


class BirthdayWishes:
    """When to wish. due() is asked at each gap; wished() records one."""

    def __init__(self, entries: list[dict] | None = None, quiet=None) -> None:
        self.entries = validate(entries or [])
        self.quiet = quiet                   # a QuietHours, or None
        self._day: date | None = None
        self._count = 0
        self._last: datetime | None = None

    def set(self, entries: list[dict]) -> None:
        self.entries = validate(entries)

    def today(self, now: datetime) -> list[dict]:
        return todays(self.entries, now.date())

    def due(self, now: datetime, clock_ok: bool) -> list[dict]:
        """The people to wish now, or [] if it's not time."""
        if not clock_ok or not self.entries:
            return []
        if self.quiet is not None and Time(now.hour, now.minute) in self.quiet:
            return []
        if self._day != now.date():
            self._day, self._count, self._last = now.date(), 0, None
        if self._count >= MAX_WISHES:
            return []
        if self._last is not None and (now - self._last).total_seconds() < WISH_EVERY_S:
            return []
        return self.today(now)

    def wished(self, now: datetime) -> None:
        self._day = now.date()
        self._count += 1
        self._last = now
