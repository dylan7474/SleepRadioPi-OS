"""News bulletins: top stories on the hour, gentler stories at half past.

Port of SleepRadio's core/news package (NewsModels, NewsSchedule, NewsText,
NewsRssParser, NewsRepository). Rule-based on purpose: it can only drop or
trim a headline's words, never invent them.

Feeds are BBC News RSS. Their terms (BBC Terms of Use s.15, checked for the
Android app 2026-09-20): personal use is fine if the feed isn't changed, the
BBC is credited as "BBC News", and no logos are used; business use needs the
BBC's permission. So this must stay free and non-commercial, and every
bulletin keeps its spoken "from BBC News" credit.
"""

from __future__ import annotations

import html
import logging
import random
import re
import threading
import time as _time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as Time
from email.utils import parsedate_to_datetime
from enum import Enum

from .script_builder import spoken_time
from .speech import cardinal_words, normalize_for_speech

log = logging.getLogger(__name__)


class NewsSlot(Enum):
    TOP_OF_HOUR = "top"
    HALF_PAST = "soft"


@dataclass(frozen=True)
class NewsHeadline:
    title: str
    summary: str = ""
    pub_date_ms: int | None = None
    source: str = ""


@dataclass(frozen=True)
class NewsFeed:
    name: str
    url: str


TOP_STORIES = [NewsFeed("BBC News", "https://feeds.bbci.co.uk/news/rss.xml")]
SOFT_STORIES = [
    NewsFeed("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    NewsFeed("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    NewsFeed("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    NewsFeed("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml"),
]


def feeds_for(slot: NewsSlot) -> list[NewsFeed]:
    return TOP_STORIES if slot == NewsSlot.TOP_OF_HOUR else SOFT_STORIES


# --- Schedule ----------------------------------------------------------------------


@dataclass(frozen=True)
class QuietHours:
    """start inclusive, end exclusive; may wrap midnight. start == end is no quiet period."""

    start: Time
    end: Time

    def __contains__(self, t: Time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    @classmethod
    def of_minutes(cls, start_min: int, end_min: int) -> "QuietHours":
        def clamp(m: int) -> Time:
            m = max(0, min(m, 24 * 60 - 1))
            return Time(m // 60, m % 60)

        return cls(clamp(start_min), clamp(end_min))


@dataclass(frozen=True)
class DueNews:
    slot: NewsSlot
    key: str
    mark: datetime


EARLY_MIN = 2
LATE_MIN = 8
PREP_LEAD_MIN = 15


class NewsSchedule:
    """Bulletins are only read in the gap between tracks, so each :00/:30 mark has a
    window: it opens EARLY_MIN minutes before and closes LATE_MIN minutes after (a stale
    bulletin is worse than none). Each mark is read at most once. A mark inside quiet
    hours is never read or prepared -- decided by the mark's own time."""

    def __init__(self, quiet: QuietHours | None = None) -> None:
        self.quiet = quiet
        self._read: set[str] = set()

    def due_at(self, now: datetime) -> DueNews | None:
        return self._window_at(now, EARLY_MIN)

    def prep_at(self, now: datetime) -> DueNews | None:
        """Same marks as due_at, opening PREP_LEAD_MIN early: fetching and
        synthesising a bulletin takes a while."""
        return self._window_at(now, PREP_LEAD_MIN)

    def mark_read(self, due: DueNews) -> None:
        self._read.add(due.key)

    def _window_at(self, now: datetime, lead_min: int) -> DueNews | None:
        minute_of_half = now.minute % 30
        base = now.replace(minute=now.minute - minute_of_half, second=0, microsecond=0)
        if minute_of_half <= LATE_MIN:
            mark = base
        elif 30 - minute_of_half <= lead_min:
            mark = base + timedelta(minutes=30)
        else:
            return None
        if self.quiet is not None and mark.time() in self.quiet:
            return None
        key = mark.isoformat()
        if key in self._read:
            return None
        slot = NewsSlot.TOP_OF_HOUR if mark.minute == 0 else NewsSlot.HALF_PAST
        return DueNews(slot, key, mark)


# --- Headlines -----------------------------------------------------------------------

MIN_HEADLINE_CHARS = 20
MAX_HEADLINE_CHARS = 170

# "Watch: ...", "Live: ..." -- labels for the web page, not for speech.
_LABEL_PREFIX = re.compile(
    r"^(watch|live|listen|video|in pictures|pictures|analysis|opinion|review|newscast|podcast)"
    r"\s*[:\-–—]\s*",
    re.IGNORECASE,
)
# Stories that only make sense on a screen.
_NOT_SPEAKABLE = re.compile(
    r"\b(live updates?|live blog|newsletter|quiz|how to watch|in pictures|photo gallery|"
    r"iplayer|bbc sounds|watch the full|watch live|listen live)\b",
    re.IGNORECASE,
)
# "Golf: PGA Championship" -- a section/event label, not a story.
_TOPIC_LABEL = re.compile(r"^\S+(?:\s\S+){0,2}:\s(?![\'\"‘“])\S+(?:\s\S+){0,3}[.!?]?$")
# Words that shouldn't reach a sleeper in the soft bulletin. Deliberately blunt:
# a false positive skips one story, a miss wakes someone up.
_GRIM = re.compile(
    r"\b(kill(s|ed|ing|er)?|dead|death|deaths|dies|died|dying|murder(s|ed|er)?|shot|shooting|"
    r"stabb(ed|ing)|terror(ist|ism)?|bomb(s|ing|ed)?|war|wars|attack(s|ed)?|crash(es|ed)?|"
    r"victims?|abus(e|ed|er|ers)|rape[sd]?|suicide|massacre|hostages?|missiles?|explosions?|"
    r"fatal(ity|ities)?|casualt(y|ies)|drones?|invasion|genocide|torture[dr]?|cancer|tumou?rs?)\b",
    re.IGNORECASE,
)


def is_grim(text: str) -> bool:
    return bool(_GRIM.search(text))


def tidy_headline(raw: str) -> str | None:
    """A raw feed title as a sentence fit to read aloud, or None if unspeakable."""
    t = re.sub(r"\s+", " ", raw).strip()
    t = _LABEL_PREFIX.sub("", t).strip()
    if not MIN_HEADLINE_CHARS <= len(t) <= MAX_HEADLINE_CHARS:
        return None
    if _NOT_SPEAKABLE.search(t) or _TOPIC_LABEL.match(t):
        return None
    t = re.sub(r"\s[-–—]\s", ", ", t)  # a spaced dash is a page-layout pause
    if t[-1] not in ".!?":
        t += "."
    return t


def news_key(title: str) -> str:
    """Identity for 'read already / near-duplicate': first six words, letters and digits only."""
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
    return " ".join(words[:6])


def pick_headlines(candidates: list[NewsHeadline], slot: NewsSlot,
                   already_read: set[str] | frozenset = frozenset(), max_: int = 3) -> list[str]:
    """Newest first within each feed, feeds interleaved, unspeakable/read/duplicate
    stories dropped, and grim ones skipped at half past. Returns tidied text."""
    per_feed: dict[str, list[NewsHeadline]] = {}
    for h in candidates:
        per_feed.setdefault(h.source, []).append(h)
    lists = [sorted(v, key=lambda h: h.pub_date_ms if h.pub_date_ms is not None else float("-inf"),
                    reverse=True) for v in per_feed.values()]
    interleaved = [lst[i] for i in range(max(map(len, lists), default=0)) for lst in lists if i < len(lst)]
    seen: set[str] = set()
    out: list[str] = []
    for h in interleaved:
        if len(out) >= max_:
            break
        tidy = tidy_headline(h.title)
        if tidy is None:
            continue
        if slot == NewsSlot.HALF_PAST and (is_grim(h.title) or is_grim(h.summary)):
            continue
        key = news_key(tidy)
        if key in already_read or key in seen:
            continue
        seen.add(key)
        out.append(tidy)
    return out


# --- Speakable news text -------------------------------------------------------------

_MONEY = re.compile(r"([£$€])(\d[\d,]*(?:\.\d+)?)\s?(bn|billion|m|million|k|thousand)?\b", re.IGNORECASE)
_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")
_PENCE = re.compile(r"\b(\d+)p\b")
_GROUPED_OR_DECIMAL = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+")


def _number_words(raw: str) -> str:
    clean = raw.replace(",", "")
    whole, _, frac = clean.partition(".")
    if not whole.isdigit() or int(whole) > 999_999:
        return raw
    words = cardinal_words(int(whole))
    if frac:
        words += " point " + " ".join(cardinal_words(int(d)) for d in frac)
    return words


def _scale_word(s: str) -> str:
    s = s.lower()
    return "billion" if s in ("bn", "billion") else "million" if s in ("m", "million") else "thousand"


def speakable_news(text: str) -> str:
    """Spell out money, percentages and pence, which normalize_for_speech would mangle."""

    def money(m: re.Match) -> str:
        unit = {"£": "pound", "$": "dollar"}.get(m.group(1), "euro")
        num = _number_words(m.group(2))
        if m.group(3):
            return f"{num} {_scale_word(m.group(3))} {unit}s"
        return f"one {unit}" if m.group(2) == "1" else f"{num} {unit}s"

    t = _MONEY.sub(money, text)
    t = _PERCENT.sub(lambda m: f"{_number_words(m.group(1))} percent", t)
    t = _PENCE.sub(lambda m: "one penny" if m.group(1) == "1" else f"{_number_words(m.group(1))} pence", t)
    t = _GROUPED_OR_DECIMAL.sub(lambda m: _number_words(m.group(0)), t)
    t = re.sub(r"(?<=[A-Za-z])\+", "", t)  # "LGBTQ+" -> "LGBTQ"
    t = re.sub(r"\s+", " ", t.replace("&", " and "))
    return t.strip()


# Every bulletin names its source: the BBC's RSS terms require the "BBC News" credit.
TOP_INTROS = [
    "Here's the news from BBC News.",
    "The headlines, from BBC News.",
    "In the news, from BBC News.",
]
SOFT_INTROS = [
    "And now, a few gentler stories from BBC News.",
    "A few softer stories from BBC News.",
    "Something a little lighter, from BBC News.",
]
OUTRO = "Now, back to the music."
_MIDDLE_SIGNPOSTS = ["Also,", "And also,", "Meanwhile,", "In other news,", "Elsewhere,"]


def signpost(stories: list[str], rng: random.Random | None = None) -> str:
    """'First up,' ... 'Also,' ... 'And finally,' so headlines don't blend together."""
    rng = rng or random.Random()
    if len(stories) < 2:
        return " ".join(stories)
    middle = _MIDDLE_SIGNPOSTS[:]
    rng.shuffle(middle)
    out = []
    for i, story in enumerate(stories):
        if i == 0:
            lead = "First up,"
        elif i == len(stories) - 1:
            lead = "And finally,"
        else:
            lead = middle.pop(0) if middle else "Also,"
        out.append(f"{lead} {story}")
    return " ".join(out)


def build_bulletin_body(slot: NewsSlot, headlines: list[str], rng: random.Random | None = None) -> str | None:
    """'<intro> <headlines...> <outro>' without the time line; None when there's nothing to say."""
    if not headlines:
        return None
    rng = rng or random.Random()
    intros = TOP_INTROS if slot == NewsSlot.TOP_OF_HOUR else SOFT_INTROS
    stories = [normalize_for_speech(speakable_news(h)) for h in headlines]
    return f"{intros[rng.randrange(len(intros))]} {signpost(stories, rng)} {OUTRO}"


JUST_GONE_MAX_MIN = 5


def bulletin_time_line(mark: datetime, now: datetime) -> str:
    """Anchored to the :00/:30 mark like a newsreader ('It's just gone half past five.'),
    falling back to the real clock only when 'just gone' would mislead."""
    minutes_past = (now - mark).total_seconds() / 60
    mark_words = spoken_time(mark.time())
    if minutes_past < 0:
        return f"It's coming up to {mark_words}."
    if minutes_past <= JUST_GONE_MAX_MIN:
        return f"It's just gone {mark_words}."
    return f"It's {spoken_time(now.time())}."


# --- Fetching ------------------------------------------------------------------------


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_news_rss(xml: str, source: str) -> list[NewsHeadline]:
    """RSS 2.0 items in document order; untitled items skipped; no channel raises."""
    channel = ET.fromstring(xml).find("channel")
    if channel is None:
        raise ValueError("no <channel> in feed")
    out = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub_ms = None
        if pub := item.findtext("pubDate"):
            try:
                pub_ms = int(parsedate_to_datetime(pub).timestamp() * 1000)
            except (TypeError, ValueError):
                pass
        out.append(NewsHeadline(title, _strip_html(item.findtext("description") or ""), pub_ms, source))
    return out


@dataclass
class NewsRepository:
    """Fetches a slot's feeds and picks what to read. A 20-minute cache means :00 and :30
    don't refetch a shared feed, and it remembers what's been read. Every failure
    (offline, HTTP error, bad XML) is 'no stories from that feed', never an exception."""

    ttl_s: float = 20 * 60
    max_remembered: int = 200
    user_agent: str = "SleepRadioPi/0.1 (Raspberry Pi; +https://github.com/dylan7474/SleepRadioPi)"
    _cache: dict = field(default_factory=dict)
    _read: dict = field(default_factory=dict)  # insertion-ordered set
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def headlines_for(self, slot: NewsSlot, max_: int = 3) -> list[str]:
        candidates = [h for feed in feeds_for(slot) for h in self._load(feed)]
        with self._lock:
            picked = pick_headlines(candidates, slot, set(self._read), max_)
        log.info("news %s: %d candidates -> %d picked", slot.value, len(candidates), len(picked))
        return picked

    def mark_read(self, spoken: list[str]) -> None:
        with self._lock:
            for s in spoken:
                self._read[news_key(s)] = None
            while len(self._read) > self.max_remembered:
                self._read.pop(next(iter(self._read)))

    def _load(self, feed: NewsFeed) -> list[NewsHeadline]:
        cached = self._cache.get(feed.url)
        if cached and _time.monotonic() - cached[0] < self.ttl_s:
            return cached[1]
        try:
            req = urllib.request.Request(feed.url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=15) as resp:
                items = parse_news_rss(resp.read().decode("utf-8", "replace"), feed.name)
        except Exception as e:  # offline, HTTP error, bad XML
            log.warning("news fetch %s failed: %s", feed.url, e)
            return cached[1] if cached else []
        self._cache[feed.url] = (_time.monotonic(), items)
        return items
