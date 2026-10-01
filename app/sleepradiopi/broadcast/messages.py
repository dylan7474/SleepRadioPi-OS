"""Messages: short notes the DJ reads between songs through the day.

Written on the web page, e.g. "You're at Willow Court, and you're safe here.
Dylan lives five minutes away and will be in to see you on Sunday." They're
kept in the config as "messages" (so they're in the saved settings file too):

    {"on": true, "every_min": 15, "offset_min": 7, "start_min": 480, "end_min": 1260,
     "date_first": true, "list": [{"text": "...", "until": "2026-10-04"}]}

Each slot -- every_min apart, offset_min past the hour (by default :07, :22,
:37 and :52, well clear of the news at :00 and :30) -- plays ONE message,
the next in turn, at the first gap between songs from that time on (until
the next slot comes round). Only between start_min and end_min (daytime),
only when the clock can be trusted (offline, the day could be wrong), and
never once a message's "until" day has passed. With date_first the DJ opens
with the day and date, which helps someone who loses track of the days.

Each message can also have its own schedule: "days" (0 Monday .. 6 Sunday:
only then), "date" ("12-25": only on that day, every year), and "times"
(["09:00", ...]: said at those times, as a moment of its own, rather than in
the rotation -- e.g. "Remember your tablets" on Sundays at 09:00, "Happy
Christmas" on 12-25). A timed message is said at the first gap within
TIMED_WINDOW_MIN of its time, once; the daytime hours don't apply to it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .birthdays import ordinal_words
from .profiles import DEFAULT

MAX_MESSAGES = 30
TEXT_MAX = 400
EVERY = (15, 20, 30, 60)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
TIMED_WINDOW_MIN = 20                 # a timed message still counts this long after its time
MAX_TIMES = 12
DEFAULTS = {"on": True, "every_min": 15, "offset_min": 7, "start_min": 8 * 60, "end_min": 21 * 60,
            "date_first": True, "list": []}


def _minutes(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 24 * 60:
        raise ValueError(f"{what} must be a time of day")
    return v


def validate(cfg) -> dict:
    """Check the settings from the page or a settings file; returns them
    cleaned up (with defaults filled in). ValueError says what's wrong."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("messages must be an object")
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    if not isinstance(out["on"], bool) or not isinstance(out["date_first"], bool):
        raise ValueError("on / date_first are true or false")
    if out["every_min"] not in EVERY or isinstance(out["every_min"], bool):
        raise ValueError(f"every_min is one of {', '.join(map(str, EVERY))}")
    offset = out["offset_min"]
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < out["every_min"]:
        raise ValueError(f"offset_min is 0-{out['every_min'] - 1}")
    out["start_min"] = _minutes(out["start_min"], "the start")
    out["end_min"] = _minutes(out["end_min"], "the end")
    items = out["list"]
    if not isinstance(items, list) or len(items) > MAX_MESSAGES:
        raise ValueError(f"up to {MAX_MESSAGES} messages")
    cleaned = []
    for m in items:
        if not isinstance(m, dict) or not isinstance(m.get("text"), str):
            raise ValueError("each message needs its words")
        text = " ".join(m["text"].split())
        if not text or len(text) > TEXT_MAX:
            raise ValueError(f"a message is 1-{TEXT_MAX} characters")
        entry = {"text": text}
        until = m.get("until")
        if until not in (None, ""):
            try:
                entry["until"] = date.fromisoformat(str(until)).isoformat()
            except ValueError:
                raise ValueError(f"“{text[:30]}…”: the last day isn't a date") from None
        if m.get("off") is True:
            entry["off"] = True
        station = m.get("station")                   # a theme's name: only on that station; none: every station
        if station not in (None, ""):
            if not isinstance(station, str) or len(station.strip()) > 40:
                raise ValueError(f"“{text[:30]}…”: the station is a theme's name")
            entry["station"] = " ".join(station.split())
        days = m.get("days") or []
        if not isinstance(days, list) or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
            raise ValueError(f"“{text[:30]}…”: days are 0 (Monday) to 6 (Sunday)")
        if days:
            entry["days"] = sorted(set(days))
        on = m.get("date")
        if on not in (None, ""):
            try:
                mm, dd = (int(x) for x in str(on).split("-"))
                date(2024, mm, dd)                       # (a leap year: 02-29 is a day)
            except (ValueError, TypeError):
                raise ValueError(f"“{text[:30]}…”: the day each year is like 12-25") from None
            entry["date"] = f"{mm:02d}-{dd:02d}"
        times = m.get("times") or []
        if not isinstance(times, list) or len(times) > MAX_TIMES:
            raise ValueError(f"“{text[:30]}…”: up to {MAX_TIMES} times")
        clean_times = []
        for t in times:
            try:
                hh, mi = (int(x) for x in str(t).split(":"))
                if not (0 <= hh < 24 and 0 <= mi < 60):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(f"“{text[:30]}…”: times are like 09:00") from None
            clean_times.append(f"{hh:02d}:{mi:02d}")
        if clean_times:
            entry["times"] = sorted(set(clean_times))
        cleaned.append(entry)
    out["list"] = cleaned
    return out


def date_line(d: date) -> str:
    """"It's Tuesday, the twenty-ninth of September." """
    return f"It's {DAYS[d.weekday()]}, the {ordinal_words(d.day)} of {MONTHS[d.month - 1]}."


class Messages:
    """When a message is due, and which one. due() is asked when a gap is
    planned (with the time the gap will come); played() records it."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = validate(cfg)
        self._done_slot: datetime | None = None
        self._turn = 0
        self._done_timed: set[str] = set()   # "text@2026-12-25 09:00": said already
        self._pending = None                  # what due() chose, for played()

    def set(self, cfg: dict) -> None:
        self.cfg = validate(cfg)

    def active(self, today: date, station: str | None = None) -> list[dict]:
        """The messages for today (whatever their times) of this station: every message
        belongs to a theme (one with none is Default's; station None -- artist radio,
        or no theme -- reads Default's)."""
        here = (station or DEFAULT).lower()
        return [m for m in self.cfg["list"]
                if not m.get("off") and (not m.get("until") or date.fromisoformat(m["until"]) >= today)
                and (m.get("station") or DEFAULT).lower() == here
                and (not m.get("days") or today.weekday() in m["days"])
                and (not m.get("date") or m["date"] == f"{today.month:02d}-{today.day:02d}")]

    def rotation(self, today: date, station: str | None = None) -> list[dict]:
        """Today's messages that take turns in the slots (the ones without times)."""
        return [m for m in self.active(today, station) if not m.get("times")]

    def _timed_due(self, at: datetime, station: str | None = None) -> tuple[dict, str] | None:
        for m in self.active(at.date(), station):
            for t in m.get("times", []):
                hh, mi = map(int, t.split(":"))
                when = at.replace(hour=hh, minute=mi, second=0, microsecond=0)
                key = f"{m['text']}@{when:%Y-%m-%d %H:%M}"
                if when <= at < when + timedelta(minutes=TIMED_WINDOW_MIN) and key not in self._done_timed:
                    return m, key
        return None

    def in_hours(self, at: datetime) -> bool:
        start, end, m = self.cfg["start_min"], self.cfg["end_min"], at.hour * 60 + at.minute
        return start <= m < end if start <= end else (m >= start or m < end)

    def slot(self, at: datetime) -> datetime:
        """The latest slot time at or before [at]."""
        every, offset = self.cfg["every_min"], self.cfg["offset_min"]
        base = at.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=offset)
        if base > at:
            base -= timedelta(hours=1)
        steps = int((at - base).total_seconds() // (every * 60))
        return base + timedelta(minutes=steps * every)

    def next_slot(self, now: datetime, station: str | None = None) -> datetime | None:
        """When the next message is due to play (for the page), or None."""
        if not self.cfg["on"]:
            return None
        timed = []
        for day in range(8):                       # the next timed one, this week
            d = (now + timedelta(days=day)).date()
            for m in self.active(d, station):
                for t in m.get("times", []):
                    hh, mi = map(int, t.split(":"))
                    when = datetime(d.year, d.month, d.day, hh, mi)
                    if when >= now:
                        timed.append(when)
        rot = self._next_rotation(now, station)
        return min([x for x in (*timed, rot) if x is not None], default=None)

    def _next_rotation(self, now: datetime, station: str | None = None) -> datetime | None:
        if not self.rotation(now.date(), station):
            return None
        t = self.slot(now)
        if self._done_slot == t or t < now - timedelta(minutes=self.cfg["every_min"]):
            t += timedelta(minutes=self.cfg["every_min"])
        for _ in range(24 * 60 // self.cfg["every_min"] + 1):
            if self.in_hours(t) and self.rotation(t.date(), station):
                return t
            t += timedelta(minutes=self.cfg["every_min"])
        return None

    def due(self, at: datetime, clock_ok: bool, station: str | None = None) -> str | None:
        """The words to say in a gap at [at] on this station (a theme's name; None: the
        main show), or None if no message is due. A timed message comes first;
        otherwise the next in the rotation."""
        self._pending = None
        if not clock_ok or not self.cfg["on"]:
            return None
        timed = self._timed_due(at, station)
        if timed is not None:
            self._pending = ("timed", timed[1])
            return self.words(timed[0]["text"], at.date())
        slot = self.slot(at)
        if slot == self._done_slot or not self.in_hours(slot):
            return None
        todays = self.rotation(at.date(), station)
        if not todays:
            return None
        self._pending = ("slot", slot)
        return self.words(todays[self._turn % len(todays)]["text"], at.date())

    def words(self, text: str, today: date) -> str:
        return f"{date_line(today)} {text}" if self.cfg["date_first"] else text

    def played(self, at: datetime) -> tuple:
        """Record that what due() chose is said; returns what unplayed() needs."""
        pending, self._pending = self._pending, None
        if pending is not None and pending[0] == "timed":
            self._done_timed.add(pending[1])
            return ("timed", pending[1])
        before = ("slot", self._done_slot, self._turn)
        self._done_slot = self.slot(at)
        self._turn += 1
        return before

    def unplayed(self, record: tuple) -> None:
        """It couldn't be said after all (the DJ wasn't ready in time): due again,
        at the next gap."""
        if record[0] == "timed":
            self._done_timed.discard(record[1])
        else:
            _, self._done_slot, self._turn = record
