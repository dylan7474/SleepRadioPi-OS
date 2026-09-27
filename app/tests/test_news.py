"""Cases ported from SleepRadio's NewsScheduleTest.kt / NewsTextTest.kt."""

import random
from datetime import datetime, timedelta
from datetime import time as T

from sleepradiopi.broadcast.news import (
    NewsHeadline, NewsSchedule, NewsSlot, QuietHours, build_bulletin_body, bulletin_time_line,
    is_grim, parse_news_rss, pick_headlines, signpost, speakable_news, tidy_headline,
)


def at(h, m, s=0, day=20):
    return datetime(2026, 9, day, h, m, s)


def test_top_of_hour_and_half_past_slots():
    assert NewsSchedule().due_at(at(17, 3)).slot == NewsSlot.TOP_OF_HOUR
    assert NewsSchedule().due_at(at(17, 31)).slot == NewsSlot.HALF_PAST


def test_window_opens_two_minutes_early_and_closes_eight_after():
    s = NewsSchedule()
    assert s.due_at(at(16, 57, 59)) is None
    assert s.due_at(at(16, 58)).key == s.due_at(at(17, 4)).key
    assert s.due_at(at(17, 38, 59)).slot == NewsSlot.HALF_PAST
    assert s.due_at(at(17, 39)) is None
    assert s.due_at(at(17, 20)) is None


def test_each_mark_is_read_once():
    s = NewsSchedule()
    s.mark_read(s.due_at(at(17, 1)))
    assert s.due_at(at(17, 5)) is None
    assert s.prep_at(at(17, 5)) is None
    assert s.due_at(at(17, 29)).slot == NewsSlot.HALF_PAST


def test_prep_opens_fifteen_minutes_early():
    s = NewsSchedule()
    assert s.prep_at(at(16, 44)) is None
    assert s.prep_at(at(16, 45)).key == s.due_at(at(17, 0)).key
    assert s.due_at(at(16, 45)) is None


def test_crosses_midnight():
    s = NewsSchedule()
    assert s.due_at(at(23, 59)).key == s.due_at(at(0, 1, day=21)).key


def test_quiet_hours_by_mark_time():
    q = QuietHours.of_minutes(23 * 60, 6 * 60)
    assert T(23, 0) in q and T(0, 0) in q and T(5, 59) in q
    assert T(6, 0) not in q and T(22, 59) not in q
    s = NewsSchedule(q)
    assert s.due_at(at(23, 1)) is None
    assert s.due_at(at(5, 59)).mark.hour == 6


def test_bulletin_time_line():
    mark = at(17, 30)
    assert bulletin_time_line(mark, mark - timedelta(minutes=1)) == "It's coming up to half past five."
    assert bulletin_time_line(mark, mark + timedelta(minutes=5)) == "It's just gone half past five."
    assert bulletin_time_line(mark, mark + timedelta(minutes=7)) == "It's twenty-five minutes to six."


def test_tidy_headline():
    assert tidy_headline("Watch: Museum finds a lost painting in the attic") == \
        "Museum finds a lost painting in the attic."
    assert tidy_headline("LIVE - Village hall reopens after repairs") == "Village hall reopens after repairs."
    assert tidy_headline("Baking show returns - host on the new series") == \
        "Baking show returns, host on the new series."
    for bad in ["Live updates: storm crosses the country", "Quiz: how well do you know the river?",
                "Short one", "x" * 200, "Golf: PGA Championship", "Tennis: US Open day two.",
                "Listen live to the debate on BBC Sounds tonight"]:
        assert tidy_headline(bad) is None, bad


def test_grim_filter_and_half_past_skips_grim():
    assert is_grim("Two people died in the crash")
    assert not is_grim("Warm weather boosts warmth of garden")
    items = [NewsHeadline("Ten people died in the storm overnight", source="A"),
             NewsHeadline("Library reopens after a long refurbishment", source="B")]
    assert pick_headlines(items, NewsSlot.HALF_PAST) == ["Library reopens after a long refurbishment."]
    assert len(pick_headlines(items, NewsSlot.TOP_OF_HOUR)) == 2


def test_already_read_and_max():
    items = [NewsHeadline(f"Story number {w} about the local area", source="A")
             for w in ["one", "two", "three", "four", "five"]]
    first = pick_headlines(items, NewsSlot.TOP_OF_HOUR, max_=2)
    from sleepradiopi.broadcast.news import news_key
    read = {news_key(h) for h in first}
    assert all(news_key(h) not in read for h in pick_headlines(items, NewsSlot.TOP_OF_HOUR, read))
    assert len(pick_headlines(items, NewsSlot.TOP_OF_HOUR, max_=5)) == 5


def test_speakable_news():
    assert speakable_news("New £1m grant.") == "New one million pounds grant."
    assert speakable_news("£2.5bn") == "two point five billion pounds"
    assert speakable_news("£1") == "one pound"
    assert speakable_news("$3 million") == "three million dollars"
    assert speakable_news("Prices up 5%") == "Prices up five percent"
    assert speakable_news("10p cut to fuel duty") == "ten pence cut to fuel duty"
    assert speakable_news("12,000 people") == "twelve thousand people"
    assert speakable_news("older LGBTQ+ people") == "older LGBTQ people"
    assert speakable_news("Smith & Sons") == "Smith and Sons"


def test_bulletin_credits_bbc_and_has_no_digits():
    b = build_bulletin_body(NewsSlot.TOP_OF_HOUR, ["Council spends £2m on 3 parks.", "Library reopens."],
                            random.Random(1))
    assert "BBC News" in b and b.endswith("Now, back to the music.")
    assert not any(c.isdigit() or c in "£$€%" for c in b)
    assert build_bulletin_body(NewsSlot.TOP_OF_HOUR, []) is None


def test_signpost():
    assert signpost(["Alpha.", "Beta."]) == "First up, Alpha. And finally, Beta."
    assert signpost(["Solo."]) == "Solo."


def test_parse_news_rss():
    xml = """<rss><channel><title>t</title>
      <item><title>Council opens new riverside walkway</title>
        <description><![CDATA[<p>The path is two miles long.</p>]]></description>
        <pubDate>Sun, 20 Sep 2026 10:00:00 GMT</pubDate></item>
      <item><title></title></item>
      <item><title>Second story here</title></item>
    </channel></rss>"""
    items = parse_news_rss(xml, "Feed")
    assert [i.title for i in items] == ["Council opens new riverside walkway", "Second story here"]
    assert items[0].summary == "The path is two miles long."
    assert items[0].pub_date_ms is not None and items[0].source == "Feed"
