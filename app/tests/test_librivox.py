import json
import urllib.error
from pathlib import Path

import pytest

from sleepradiopi.playback import librivox as lv
from sleepradiopi.playback import podcasts as pod_mod
from sleepradiopi.playback.audiobooks import Positions

BOOK = json.loads((Path(__file__).parent / "librivox_book.json").read_text())   # real, trimmed to 3 chapters


class Feed:
    """A fake LibriVox: records the addresses asked for."""

    def __init__(self, book=BOOK, fail=False):
        self.book, self.fail, self.urls = book, fail, []

    def __call__(self, url, limit, headers=None):
        self.urls.append(url)
        if self.fail:
            raise OSError("offline")
        if "extended=1" in url:
            return json.dumps({"books": [self.book]}).encode()
        return json.dumps({"books": [{k: v for k, v in self.book.items() if k != "sections"}]}).encode()


def test_clean_text_strips_html():
    assert lv.clean_text("<p>The <em>very</em> best&nbsp;&amp; <br/>more</p>") == "The very best & more"


def test_https_is_forced_and_junk_dropped():
    assert lv.https("http://www.archive.org/x.mp3") == "https://www.archive.org/x.mp3"
    assert lv.https("") == "" and lv.https(None) == "" and lv.https("ftp://a/b") == ""


def test_book_parsed_from_real_data():
    b = lv.parse_book(BOOK)
    assert b["id"] == "253" and b["title"] == "Pride and Prejudice" and b["author"] == "Jane Austen"
    assert b["duration_ms"] == 47204000 and "<" not in b["about"]
    assert b["cover"] == "https://archive.org/services/img/pride_and_prejudice_librivox"


def test_chapters_in_order_with_https_and_seconds_to_ms():
    chs = lv.parse_chapters(BOOK)
    assert [c["number"] for c in chs] == [1, 2, 3]
    assert chs[0]["duration_ms"] == 1132000 and chs[0]["url"].startswith("https://")
    assert chs[0]["reader"] == "Chris Goringe"


def test_sections_without_audio_are_skipped_and_http_upgraded():
    raw = {"sections": [
        {"id": "1", "section_number": "1", "title": "A", "listen_url": "http://archive.org/a.mp3", "playtime": "10"},
        {"id": "2", "section_number": "2", "title": "B", "listen_url": "", "playtime": "10"},
        {"id": "3", "section_number": "3", "title": "", "listen_url": None, "playtime": "x"},
        {"id": "4", "section_number": "4", "title": "D", "listen_url": "https://archive.org/d.mp3", "playtime": "bad"},
    ]}
    chs = lv.parse_chapters(raw)
    assert [c["id"] for c in chs] == ["1", "4"]
    assert chs[0]["url"] == "https://archive.org/a.mp3" and chs[1]["duration_ms"] == 0


def pods(tmp_path, feed=None):
    pos = Positions(tmp_path / "pos.json")
    return pod_mod.Podcasts([], tmp_path / "pods", pos, book_fetch=feed or Feed()), pos


def test_search_title_uses_prefix_and_author_the_surname():
    feed = Feed()
    books = lv.search("pride", fetch=feed)
    lv.search("Jane Austen", "author", fetch=feed)
    assert "title=%5Epride" in feed.urls[0] and "limit=20&offset=0" in feed.urls[0]
    assert "author=Austen" in feed.urls[1]
    assert books[0]["feed_url"] == "librivox:253"
    lv.search("pride", offset=20, fetch=feed)
    assert "offset=20" in feed.urls[2]


def test_search_nothing_found_is_an_empty_list():
    assert lv.search("zzzz", fetch=lambda url, limit, headers=None: b'{"error":"Audiobooks could not be found"}') == []
    assert lv.search("  ") == []


def test_search_offline_says_so_and_only_title_or_author():
    with pytest.raises(lv.LibriVoxError, match="online"):
        lv.search("pride", fetch=Feed(fail=True))
    with pytest.raises(lv.LibriVoxError):
        lv.search("pride", "genre")


def test_feed_is_a_podcast_shaped_serial_newest_chapter_first():
    f = lv.feed("253", Feed())
    assert f["serial"] and f["title"] == "Pride and Prejudice" and f["author"] == "Jane Austen"
    assert [e["pub"] for e in f["episodes"]] == [3.0, 2.0, 1.0]
    assert f["episodes"][-1]["url"].startswith("https://") and f["episodes"][-1]["duration_ms"] == 1132000
    assert f["episodes"][-1]["about"] == "Read by Chris Goringe"


def test_unknown_book_and_empty_book():
    with pytest.raises(lv.LibriVoxError, match="no such"):
        lv.feed("1", lambda u, l, h=None: b'{"error":"Audiobooks could not be found"}')
    with pytest.raises(lv.LibriVoxError, match="no chapters"):
        lv.feed("253", Feed(dict(BOOK, sections=[])))
    with pytest.raises(lv.LibriVoxError):
        lv.book_id("librivox:../x")


def test_following_a_book_reads_in_order_and_remembers_places(tmp_path):
    p, pos = pods(tmp_path)
    show = p.subscribe("librivox:253")
    assert show["title"] == "Pride and Prejudice"
    sid = show["id"]
    eps = p.episodes(sid)                                  # newest first, as for any show
    assert [e["title"] for e in eps][-1] == "Chapters 1-3"
    first, second, third = eps[::-1]
    assert p.pick(sid)["guid"] == first["guid"]            # a new book starts at chapter 1, not the last
    pos.set(pod_mod.episode_key(sid, first["guid"]), 1, done=True)
    assert p.pick(sid)["guid"] == second["guid"]
    assert p.next_after(sid, second["guid"])["guid"] == third["guid"]
    assert p.next_after(sid, third["guid"]) is None        # after the last chapter the radio pauses
    assert p.summary()[0]["serial"] is True


def test_a_followed_book_is_read_offline_and_not_refetched_in_the_background(tmp_path):
    feed = Feed()
    p, _ = pods(tmp_path, feed)
    sid = p.subscribe("librivox:253")["id"]
    n = len(feed.urls)
    p.refresh_all()
    assert len(feed.urls) == n
    again = pod_mod.Podcasts(p.shows, tmp_path / "pods", Positions(None), book_fetch=Feed(fail=True))
    assert len(again.episodes(sid)) == 3                   # the saved copy
    assert again.refresh(sid)["title"] == "Pride and Prejudice"


def test_following_is_remembered_in_the_settings():
    shows = pod_mod.validate_shows([{"feed_url": "librivox:253", "title": "Pride and Prejudice"}])
    assert shows[0]["feed_url"] == "librivox:253"
    with pytest.raises(ValueError):
        pod_mod.validate_shows([{"feed_url": "ftp://x"}])


