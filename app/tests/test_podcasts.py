import functools
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.playback import podcasts as pod_mod
from sleepradiopi.playback.audiobooks import Positions
from sleepradiopi.web.server import make_handler

from test_offline import _station

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Night Talk</title>
  <itunes:author>BBC Radio 4</itunes:author>
  <item><title>Episode 2</title><guid>ep-2</guid><pubDate>Tue, 02 Jan 2024 06:00:00 GMT</pubDate>
    <enclosure url="{base}/2.mp3" type="audio/mpeg" length="9000"/><itunes:duration>00:01</itunes:duration>
    <description>&lt;p&gt;The &lt;b&gt;second&lt;/b&gt; one&lt;/p&gt;</description></item>
  <item><title>Episode 1</title><guid>ep-1</guid><pubDate>Mon, 01 Jan 2024 06:00:00 GMT</pubDate>
    <enclosure url="{base}/1.mp3" type="audio/mpeg"/><itunes:duration>1</itunes:duration></item>
  <item><title>No audio</title><guid>ep-x</guid><pubDate>Mon, 01 Jan 2024 07:00:00 GMT</pubDate></item>
  <item><title>Episode 3</title><guid>ep-3</guid><pubDate>Wed, 03 Jan 2024 06:00:00 GMT</pubDate>
    <enclosure url="{base}/3.mp3" type="audio/mpeg"/><itunes:duration>0:00:01</itunes:duration></item>
</channel>
</rss>
"""


def _feed(base: str = "http://pods") -> bytes:
    return FEED.format(base=base).encode()


def _pods(tmp_path: Path, feed: bytes | None = None) -> pod_mod.Podcasts:
    pods = pod_mod.Podcasts([], tmp_path / "cache", Positions(tmp_path / "places.json"),
                            fetch=lambda url, limit, headers=None: feed or _feed())
    pods.subscribe("http://pods/feed.xml")
    return pods


def test_a_feed_is_read_newest_first() -> None:
    feed = pod_mod.parse_feed(_feed(), "http://pods/feed.xml")
    assert feed["title"] == "Night Talk" and feed["author"] == "BBC Radio 4"
    assert [e["guid"] for e in feed["episodes"]] == ["ep-3", "ep-2", "ep-1"]      # (no audio: left out)
    two = feed["episodes"][1]
    assert two["duration_ms"] == 1000 and two["bytes"] == 9000 and two["about"] == "The second one"
    assert pod_mod.is_mp3(two)


@pytest.mark.parametrize("bad", [b"not xml", b"<html><body/></html>",
                                 b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss/>'])
def test_what_isnt_a_feed(bad) -> None:
    with pytest.raises(pod_mod.PodcastError):
        pod_mod.parse_feed(bad)


def test_durations() -> None:
    assert pod_mod.parse_duration("1:02:03") == 3_723_000
    assert pod_mod.parse_duration("45:00") == 2_700_000
    assert pod_mod.parse_duration("90") == 90_000
    assert pod_mod.parse_duration("") == 0


def test_search_reads_the_directory() -> None:
    asked = []

    def fetch(url, limit, headers=None):
        asked.append(url)
        return json.dumps({"results": [
            {"collectionName": "In Our Time", "artistName": "BBC", "feedUrl": "https://feeds/iot.rss"},
            {"collectionName": "In Our Time again", "feedUrl": "https://feeds/iot.rss"},      # (the same feed)
            {"collectionName": "No feed"},
            {"collectionName": "Odd", "feedUrl": "file:///etc/passwd"}]}).encode()
    found = pod_mod.search("in our time", fetch)
    assert "term=in%20our%20time" in asked[0]
    assert found == [{"id": pod_mod.feed_id("https://feeds/iot.rss"), "feed_url": "https://feeds/iot.rss",
                      "title": "In Our Time", "author": "BBC"}]
    assert pod_mod.search("  ", fetch) == []

    def offline(url, limit, headers=None):
        raise OSError("no network")
    with pytest.raises(pod_mod.PodcastError):
        pod_mod.search("x", offline)


def test_following_saves_and_caches(tmp_path: Path) -> None:
    saved = []
    pods = pod_mod.Podcasts([], tmp_path / "cache", Positions(None), save=saved.append,
                            fetch=lambda url, limit, headers=None: _feed())
    show = pods.subscribe("http://pods/feed.xml")
    assert show["title"] == "Night Talk" and saved[-1] == [show]
    # a restart, offline: the episode list comes from the cache
    again = pod_mod.Podcasts(saved[-1], tmp_path / "cache", Positions(None),
                             fetch=lambda url, limit, headers=None: (_ for _ in ()).throw(OSError("offline")))
    assert [e["title"] for e in again.episodes(show["id"])] == ["Episode 3", "Episode 2", "Episode 1"]
    assert again.refresh(show["id"])["title"] == "Night Talk"
    pods.unsubscribe(show["id"])
    assert saved[-1] == [] and pods.show(show["id"]) is None
    with pytest.raises(pod_mod.PodcastError):
        pods.subscribe("ftp://pods/feed.xml")


def test_which_episode_a_button_plays(tmp_path: Path) -> None:
    pods = _pods(tmp_path)
    sid = pods.shows[0]["id"]
    key = lambda guid: pod_mod.episode_key(sid, guid)
    assert pods.pick(sid)["guid"] == "ep-3"                       # nothing heard: the newest
    pods.positions.set(key("ep-1"), 400)
    assert pods.pick(sid)["guid"] == "ep-1"                       # the one part-heard
    # from a starting episode: in order, oldest to newest, skipping those heard
    gid1 = pod_mod.guid_id("ep-1")
    assert pods.pick(sid, gid1)["guid"] == "ep-1"
    pods.positions.set(key("ep-1"), 1000, done=True)
    assert pods.pick(sid, gid1)["guid"] == "ep-2"
    assert pods.pick(sid, "ep-1")["guid"] == "ep-2"               # (the feed's own id works too)
    pods.positions.set(key("ep-2"), 1000, done=True)
    pods.positions.set(key("ep-3"), 1000, done=True)
    assert pods.pick(sid, gid1)["guid"] == "ep-3"                 # all heard: the latest
    assert pods.pick(sid, "gone")["guid"] == "ep-3"               # (a start no longer in the feed)
    assert pods.next_after(sid, "ep-1")["guid"] == "ep-2"
    assert pods.next_after(sid, "ep-2")["guid"] == "ep-3"
    assert pods.next_after(sid, "ep-3") is None                   # the latest: stop there


def test_a_podcast_button_starts_at_its_episode(tmp_path: Path) -> None:
    st = _station(tmp_path)
    st.podcasts = _pods(tmp_path)
    sid = st.podcasts.shows[0]["id"]
    p = presets_mod.Presets(st, None, None, [
        {"kind": "podcast", "show": sid, "title": "Night Talk", "start": pod_mod.guid_id("ep-1"),
         "start_title": "Episode 1"}])
    assert p.status()["buttons"][0]["label"] == "Night Talk"
    p.press(0)
    assert st.source["kind"] == "episode" and st.source["guid"] == "ep-1" and st.source["title"] == "Episode 1"
    held = presets_mod.Presets(st, None, None, [])
    now = held.current()
    assert now == {"kind": "podcast", "show": sid, "title": "Night Talk", "start": pod_mod.guid_id("ep-1"),
                   "start_title": "Episode 1"}
    with pytest.raises(ValueError):
        st.tune({"kind": "episode", "show": "pod_nothere", "guid": "ep-1"})


def _mp3(path: Path, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "32k", str(path)], check=True)


@pytest.fixture
def web(tmp_path: Path):
    """A little web server with the feed and its episodes."""
    root = tmp_path / "www"
    root.mkdir()
    for n in (1, 2, 3):
        _mp3(root / f"{n}.mp3", 1)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(root)))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "feed.xml").write_bytes(_feed(base))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield base
    httpd.shutdown()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args): ...


class Out:
    def __init__(self):
        self.blocks = 0

    def start(self): ...
    def stop(self): ...

    def write(self, block):
        self.blocks += 1


def test_episodes_play_on_in_order_to_the_latest(tmp_path: Path, web, monkeypatch) -> None:
    monkeypatch.setattr(station_mod, "RADIO_PREBUFFER_S", 0.1)
    st = _station(tmp_path)
    st.tts = None
    st.podcasts = pod_mod.Podcasts([], tmp_path / "cache", st.book_positions)
    sid = st.podcasts.subscribe(web + "/feed.xml")["id"]
    ended = []
    st._listeners = 1
    st.on_book_end = lambda: (ended.append(True), st._stop.set())
    st.output = Out()
    st.play_episode(sid, start=pod_mod.guid_id("ep-1"))
    played = []
    st._switch.clear()
    while st.source is not None and not st._stop.is_set() and len(played) < 5:
        played.append(st.source["guid"])
        st._switch.clear()
        st._run_episode(st.source)
    assert played == ["ep-1", "ep-2", "ep-3"] and ended == [True]
    assert all(st.book_positions.done(pod_mod.episode_key(sid, g)) for g in played)
    assert st.output.blocks > 20


def test_the_podcast_api(tmp_path: Path, web) -> None:
    st = _station(tmp_path)
    st.tts = None
    st.podcasts = pod_mod.Podcasts([], tmp_path / "cache", st.book_positions)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, d = call("/api/podcasts/follow", {"feed_url": web + "/feed.xml"})
        assert code == 200 and d["show"]["title"] == "Night Talk"
        sid = d["show"]["id"]
        code, d = call("/api/podcasts")
        assert code == 200 and d["shows"][0]["episodes"] == 3 and d["shows"][0]["latest"]["title"] == "Episode 3"
        code, d = call(f"/api/podcasts/episodes?id={sid}")
        assert code == 200 and [e["gid"] for e in d["episodes"]] == [pod_mod.guid_id(g) for g in ("ep-3", "ep-2", "ep-1")]
        code, d = call("/api/podcasts/heard", {"id": sid, "guid": "ep-3", "heard": True})
        assert code == 200 and st.book_positions.done(pod_mod.episode_key(sid, "ep-3"))
        code, d = call("/api/podcasts/play", {"id": sid, "guid": "ep-2"})
        assert code == 200, d
        assert d["source"]["title"] == "Episode 2" and d["source"]["show_title"] == "Night Talk"
        code, d = call("/api/podcasts/play", {"id": "pod_nothere"})
        assert code == 400
        code, d = call("/api/podcasts/unfollow", {"id": sid})
        assert code == 200 and st.podcasts.shows == []
    finally:
        httpd.shutdown()
