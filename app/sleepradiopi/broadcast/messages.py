"""Messages: short notes the DJ reads between songs, each a theme's own.

Written on the web page, e.g. "You're at Willow Court, and you're safe here.
Dylan lives five minutes away and will be in to see you on Sunday." Kept in
the config as "messages" (so they're in the saved settings file too):

    {"on": true, "list": [{"text": "...", "station": "Carisbrooke", "every": 15,
                           "from": "08:00", "to": "21:00", "date_first": true}]}

Every message has its own timing: "every" N minutes (15, 20, 30, 60 or 120;
the slots are OFFSET_MIN past the hour -- :07, :22, :37, :52 for 15, well
clear of the news at :00 and :30), between "from" and "to", read at the
first gap from its slot on (until its next slot); or "times" (["09:00", ...]:
said at those times, as a moment of its own, at the first gap within
TIMED_WINDOW_MIN; its hours don't apply). Also "days" (0 Monday .. 6 Sunday),
"date" ("12-25": only that day each year), "until" (a last day), "off", and
"date_first" (the DJ opens with the day and date, which helps someone who
loses track of the days). Only when the clock can be trusted (offline, the
day could be wrong). When two are due in one gap, one is read (a timed one
first, else the one said longest ago) and the other waits for the next gap.
(Settings from before each message had its own timing: the old shared ones,
"every_min", "start_min", "end_min", "date_first", are given to each.)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .birthdays import ordinal_words
from .profiles import DEFAULT

MAX_MESSAGES = 30
TEXT_MAX = 400
EVERY = (15, 20, 30, 60, 120)
OFFSET_MIN = 7                        # slots this far past the hour (clear of the news at :00 and :30)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
TIMED_WINDOW_MIN = 20                 # a timed message still counts this long after its time
MAX_TIMES = 12
DEFAULTS = {"on": True, "every_min": 15, "offset_min": 7, "start_min": 8 * 60, "end_min": 21 * 60,
            "date_first": True, "list": []}


def _clock(v, what: str) -> str:
    """A time of day, "HH:MM" (or minutes, from the old shared settings)."""
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 24 * 60:
        return f"{min(v, 24 * 60 - 1) // 60:02d}:{min(v, 24 * 60 - 1) % 60:02d}" if v < 24 * 60 else "23:59"
    try:
        hh, mi = (int(x) for x in str(v).split(":"))
        if 0 <= hh < 24 and 0 <= mi < 60:
            return f"{hh:02d}:{mi:02d}"
    except (ValueError, TypeError):
        pass
    raise ValueError(f"{what} is a time of day, like 08:00")


def _min(hhmm: str) -> int:
    hh, mi = hhmm.split(":")
    return int(hh) * 60 + int(mi)


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
    legacy = {"every": out["every_min"], "from": out["start_min"], "to": out["end_min"], "date_first": out["date_first"]}
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
        every = m.get("every", legacy["every"])
        if every not in EVERY or isinstance(every, bool):
            raise ValueError(f"“{text[:30]}…”: every is one of {', '.join(map(str, EVERY))} minutes")
        entry["every"] = every
        for key in ("from", "to"):
            entry[key] = _clock(m.get(key, legacy[key]), f"“{text[:30]}…”: {key}")
        df = m.get("date_first", legacy["date_first"])
        if not isinstance(df, bool):
            raise ValueError(f"“{text[:30]}…”: date_first is true or false")
        entry["date_first"] = df
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
    """When a message is due, and which. due() is asked when a gap is planned (with
    the time the gap will come); played() records it, unplayed() takes it back."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = validate(cfg)
        self._done: set[str] = set()          # "text@2026-10-01 09:07": said in that slot / at that time
        self._last: dict[str, datetime] = {}   # text -> when it was last said (who's waited longest)
        self._pending = None                  # what due() chose, for played()

    def set(self, cfg: dict) -> None:
        self.cfg = validate(cfg)

    def active(self, today: date, station: str | None = None) -> list[dict]:
        """Today's messages of this station (whatever their times): every message
        belongs to a theme (one with none is Default's; station None -- artist radio,
        or no theme -- reads Default's)."""
        here = (station or DEFAULT).lower()
        return [m for m in self.cfg["list"]
                if not m.get("off") and (not m.get("until") or date.fromisoformat(m["until"]) >= today)
                and (m.get("station") or DEFAULT).lower() == here
                and (not m.get("days") or today.weekday() in m["days"])
                and (not m.get("date") or m["date"] == f"{today.month:02d}-{today.day:02d}")]

    @staticmethod
    def slot(m: dict, at: datetime) -> datetime | None:
        """The message's latest slot at or before [at] (every N minutes, OFFSET_MIN past),
        if it's within its hours; else None."""
        t = at.hour * 60 + at.minute - OFFSET_MIN
        if t < 0:
            return None
        start = at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=OFFSET_MIN + t // m["every"] * m["every"])
        lo, hi, mins = _min(m["from"]), _min(m["to"]), start.hour * 60 + start.minute
        inside = lo <= mins < hi if lo <= hi else (mins >= lo or mins < hi)
        return start if inside else None

    def _due_list(self, at: datetime, station: str | None) -> list[tuple[dict, str]]:
        """(message, key) of each message due at [at]: timed ones first, then the rest,
        the one said longest ago first."""
        timed, rest = [], []
        for m in self.active(at.date(), station):
            if m.get("times"):
                for t in m["times"]:
                    hh, mi = map(int, t.split(":"))
                    when = at.replace(hour=hh, minute=mi, second=0, microsecond=0)
                    key = f"{m['text']}@{when:%Y-%m-%d %H:%M}"
                    if when <= at < when + timedelta(minutes=TIMED_WINDOW_MIN) and key not in self._done:
                        timed.append((m, key))
            else:
                start = self.slot(m, at)
                key = start and f"{m['text']}@{start:%Y-%m-%d %H:%M}"
                if start is not None and key not in self._done:
                    rest.append((m, key))
        rest.sort(key=lambda mk: self._last.get(mk[0]["text"], datetime.min))
        return timed + rest

    def next_slot(self, now: datetime, station: str | None = None) -> datetime | None:
        """When the next message of this station is due (for the page), or None."""
        if not self.cfg["on"]:
            return None
        best = None
        for day in range(8):
            d = (now + timedelta(days=day)).date()
            midnight = datetime(d.year, d.month, d.day)
            for m in self.active(d, station):
                if m.get("times"):
                    cands = [midnight + timedelta(minutes=_min(t)) for t in m["times"]]
                else:
                    cands = [midnight + timedelta(minutes=OFFSET_MIN + k * m["every"]) for k in range((24 * 60) // m["every"] + 1)]
                    cands = [c for c in cands if c.date() == d and self.slot(m, c) == c]
                for c in cands:
                    key = f"{m['text']}@{c:%Y-%m-%d %H:%M}"
                    if (c >= now or (not m.get("times") and self.slot(m, now) == c)) and key not in self._done:
                        best = c if best is None or c < best else best
            if best is not None:
                return best
        return best

    def due(self, at: datetime, clock_ok: bool, station: str | None = None) -> str | None:
        """The words to say in a gap at [at] on this station (a theme's name; None:
        artist radio), or None if no message is due."""
        self._pending = None
        if not clock_ok or not self.cfg["on"]:
            return None
        due = self._due_list(at, station)
        if not due:
            return None
        m, key = due[0]
        self._pending = (m["text"], key)
        return self.words(m, at.date())

    def words(self, m, today: date) -> str:
        if isinstance(m, str):                       # (the text alone: as the radio-wide default)
            return f"{date_line(today)} {m}"
        return f"{date_line(today)} {m['text']}" if m.get("date_first", True) else m["text"]

    def played(self, at: datetime) -> tuple:
        """Record that what due() chose is said; returns what unplayed() needs."""
        pending, self._pending = self._pending, None
        if pending is None:
            return ("none",)
        text, key = pending
        before = ("key", key, text, self._last.get(text))
        self._done.add(key)
        self._last[text] = at
        if len(self._done) > 2000:                   # (a day or two of slots is plenty to remember)
            self._done = {k for k in self._done if k.rsplit("@", 1)[1] >= f"{at - timedelta(days=2):%Y-%m-%d}"}
        return before

    def unplayed(self, record: tuple) -> None:
        """It couldn't be said after all (the DJ wasn't ready in time): due again,
        at the next gap."""
        if record[0] != "key":
            return
        _, key, text, last = record
        self._done.discard(key)
        if last is None:
            self._last.pop(text, None)
        else:
            self._last[text] = last
