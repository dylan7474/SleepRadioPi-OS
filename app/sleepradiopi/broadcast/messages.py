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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .birthdays import ordinal_words

MAX_MESSAGES = 30
TEXT_MAX = 400
EVERY = (15, 20, 30, 60)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
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

    def set(self, cfg: dict) -> None:
        self.cfg = validate(cfg)

    def active(self, today: date) -> list[dict]:
        return [m for m in self.cfg["list"]
                if not m.get("off") and (not m.get("until") or date.fromisoformat(m["until"]) >= today)]

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

    def next_slot(self, now: datetime) -> datetime | None:
        """When the next message is due to play (for the page), or None."""
        if not self.cfg["on"] or not self.active(now.date()):
            return None
        t = self.slot(now)
        if self._done_slot == t or t < now - timedelta(minutes=self.cfg["every_min"]):
            t += timedelta(minutes=self.cfg["every_min"])
        for _ in range(24 * 60 // self.cfg["every_min"] + 1):
            if self.in_hours(t) and self.active(t.date()):
                return t
            t += timedelta(minutes=self.cfg["every_min"])
        return None

    def due(self, at: datetime, clock_ok: bool) -> str | None:
        """The words to say in a gap at [at], or None if no message is due."""
        if not clock_ok or not self.cfg["on"]:
            return None
        slot = self.slot(at)
        if slot == self._done_slot or not self.in_hours(slot):
            return None
        todays = self.active(at.date())
        if not todays:
            return None
        return self.words(todays[self._turn % len(todays)]["text"], at.date())

    def words(self, text: str, today: date) -> str:
        return f"{date_line(today)} {text}" if self.cfg["date_first"] else text

    def played(self, at: datetime) -> None:
        self._done_slot = self.slot(at)
        self._turn += 1
