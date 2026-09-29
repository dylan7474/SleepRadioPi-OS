import io
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load
from sleepradiopi.playback import radio
from sleepradiopi.web.server import make_handler

from test_offline import _station

BLOCK = np.full((4096, 2), 3000, np.int16)


# --- parsing ------------------------------------------------------------------------------

def test_playlists() -> None:
    pls = "[playlist]\nNumberOfEntries=2\nFile1=http://a.example/live\nTitle1=A\nFile2=http://b.example/\n"
    assert radio.playlist_urls(pls, "http://x/") == ["http://a.example/live", "http://b.example/"]
    m3u = "#EXTM3U\n#EXTINF:-1,Station\nhttps://s.example/stream.mp3\nrelative.aac\n"
    assert radio.playlist_urls(m3u, "http://h.example/dir/list.m3u") == [
        "https://s.example/stream.mp3", "http://h.example/dir/relative.aac"]


def test_hls_master_picks_the_best_variant_that_fits() -> None:
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=320000\nhi.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS=\"mp4a\"\nmid.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=48000\nlo.m3u8\n")
    assert radio.is_hls(master)
    assert radio.hls_variant(master, "http://h/live/master.m3u8") == "http://h/live/mid.m3u8"
    assert radio.hls_variant("#EXTM3U\n#EXT-X-TARGETDURATION:6\na.ts\n", "http://h/") is None


def test_hls_media_playlist() -> None:
    pl = radio.hls_media("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:41\n#EXT-X-TARGETDURATION:6\n"
                         "#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.4,\ns41.ts\n#EXTINF:6.4,\ns42.ts\n",
                         "http://h/a/list.m3u8")
    assert pl["seq"] == 41 and pl["target"] == 6.0 and pl["map"] == "http://h/a/init.mp4"
    assert pl["segments"] == ["http://h/a/s41.ts", "http://h/a/s42.ts"]
    assert not pl["encrypted"] and not pl["ended"]
    assert radio.hls_media("#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n", "http://h/")["encrypted"]


def test_icy_title() -> None:
    assert radio.icy_title(b"StreamTitle='Nick Drake - Pink Moon';StreamUrl='';\0\0") == "Nick Drake - Pink Moon"
    assert radio.icy_title(b"StreamTitle='';\0") == ""
    assert radio.icy_title(b"\0" * 16) is None


def test_station_checks() -> None:
    assert radio.validate_station({"name": " R4 ", "url": " https://x.example/s "}) == {
        "name": "R4", "url": "https://x.example/s", "info": ""}
    for bad in ({"name": "", "url": "http://x/"}, {"name": "A", "url": "ftp://x/"},
                {"name": "A", "url": "x.example"}, {"name": "A"}, "http://x/"):
        with pytest.raises(ValueError):
            radio.validate_station(bad)
    twice = [{"name": "A", "url": "http://a/"}, {"name": "A again", "url": "http://a/"}]
    assert [s["name"] for s in radio.validate_stations(twice)] == ["A"]
    with pytest.raises(ValueError):
        radio.validate_stations([{"name": "A", "url": f"http://a/{i}"} for i in range(radio.MAX_STATIONS + 1)])
    assert radio.validate_stations(radio.DEFAULT_STATIONS) == radio.DEFAULT_STATIONS


def test_leveller_brings_a_loud_station_down_gently() -> None:
    lev = radio.Leveller()
    loud = np.full((4096, 2), 12000, np.int16)          # ~-9 dBFS: far louder than the show
    first = lev.process(loud)
    assert np.abs(first).max() < 5000                   # turned down from the first block
    for _ in range(200):
        out = lev.process(loud)
    rms = float(np.sqrt(np.mean((out.astype(np.float32) / 32768) ** 2)))
    assert rms == pytest.approx(pcm.LOUDNESS_TARGET_RMS, rel=0.1)
    assert np.array_equal(lev.process(np.zeros((10, 2), np.int16)), np.zeros((10, 2), np.int16))


# --- search -----------------------------------------------------------------------------

class _Reply(io.BytesIO):
    def __init__(self, data: bytes, headers: dict | None = None, url: str = "") -> None:
        super().__init__(data)
        self.headers, self._url = headers or {}, url

    def geturl(self) -> str:
        return self._url


def test_search_reads_radio_browser_like_the_app() -> None:
    rows = [
        {"name": "BBC Radio 4", "url": "http://r4/pls", "url_resolved": "http://r4/stream",
         "codec": "AAC", "bitrate": 128, "country": "The United Kingdom"},
        {"name": "Same stream", "url_resolved": "http://r4/stream"},
        {"name": "No stream", "url": ""},
        {"name": "Plain", "url": "https://plain/", "codec": "UNKNOWN", "bitrate": 0},
    ]
    asked = []

    def fetch(url, headers=None):
        asked.append(url)
        return _Reply(json.dumps(rows).encode())

    found = radio.search("radio 4", fetch=fetch)
    assert "name=radio%204" in asked[0] and "hidebroken=true" in asked[0] and "order=clickcount" in asked[0]
    assert found == [
        {"name": "BBC Radio 4", "url": "http://r4/stream", "info": "AAC · 128k · The United Kingdom"},
        {"name": "Plain", "url": "https://plain/", "info": "Internet radio"},
    ]
    assert radio.search("  ", fetch=fetch) == []


def test_search_offline() -> None:
    def fetch(url, headers=None):
        raise urllib.error.URLError(OSError("Name or service not known"))
    with pytest.raises(radio.StreamError, match="online"):
        radio.search("x", fetch=fetch)


# --- real streams from a local server ------------------------------------------------------

def _encode(tmp_path: Path, name: str, args: list[str]) -> Path:
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=4", *args, str(out)], check=True)
    return out


def _serve(routes: dict):
    """routes: path -> (headers, body bytes)."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): ...

        def do_GET(self):
            if self.path not in routes:
                self.send_error(404)
                return
            headers, body = routes[self.path]
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _drain(stream: radio.RadioStream, limit_s: float = 15) -> float:
    frames, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < limit_s:
        b = stream.read(0.1)
        if b is not None:
            frames += len(b)
        elif stream.ended:
            break
    return frames / pcm.SAMPLE_RATE


def test_icy_stream_through_a_playlist(tmp_path: Path) -> None:
    mp3 = _encode(tmp_path, "a.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]).read_bytes()
    metaint = 8000
    meta = b"StreamTitle='Artist - Song';"
    meta += b"\0" * (-len(meta) % 16)
    body = b""
    for i in range(0, len(mp3), metaint):
        part = mp3[i:i + metaint]
        body += part + ((bytes([len(meta) // 16]) + meta) if len(part) == metaint else b"")
    routes = {"/live": ({"Content-Type": "audio/mpeg", "icy-metaint": str(metaint)}, body)}
    httpd, base = _serve(routes)
    routes["/listen.pls"] = ({"Content-Type": "audio/x-scpls"}, f"[playlist]\nFile1={base}/live\n".encode())
    try:
        s = radio.RadioStream(base + "/listen.pls")
        s.start()
        heard = _drain(s)
        assert heard == pytest.approx(4.0, abs=0.3)         # all of it, and no metadata as noise
        assert s.title == "Artist - Song"
        assert s.ended == "the station stopped sending"
        s.close()
    finally:
        httpd.shutdown()


def test_hls_stream(tmp_path: Path) -> None:
    hls = tmp_path / "hls"
    hls.mkdir()
    _encode(hls, "list.m3u8", ["-c:a", "aac", "-b:a", "64k", "-f", "hls", "-hls_time", "1",
                               "-hls_list_size", "0", "-hls_segment_filename", str(hls / "s%d.ts")])
    routes = {f"/hls/{p.name}": ({"Content-Type": "application/vnd.apple.mpegurl" if p.suffix == ".m3u8"
                                  else "video/mp2t"}, p.read_bytes()) for p in hls.iterdir()}
    n_segments = len([p for p in hls.iterdir() if p.suffix == ".ts"])
    httpd, base = _serve(routes)
    try:
        s = radio.RadioStream(base + "/hls/list.m3u8")
        s.start()
        heard = _drain(s)
        # Joined near the live edge: the last few segments, not the lot.
        assert 1.0 < heard < 4.0 and n_segments > radio.HLS_LIVE_EDGE
        s.close()
    finally:
        httpd.shutdown()


def test_unreachable_station() -> None:
    s = radio.RadioStream("http://127.0.0.1:9/nothing")
    s.start()
    assert _drain(s, 10) == 0
    assert s.ended and "reach" in s.ended
    s.close()


# --- the station -------------------------------------------------------------------------

class FakeStream:
    """Plays [blocks] blocks, then ends."""
    made: list = []

    def __init__(self, url, blocks=30, title="Now - This"):
        self.url, self.left, self.title, self.ended = url, blocks, title, None
        FakeStream.made.append(self)

    def start(self): ...
    def close(self): ...

    @property
    def buffered_s(self):
        return self.left * 4096 / pcm.SAMPLE_RATE

    def read(self, timeout=0.1):
        if self.left <= 0:
            self.ended = "the station stopped sending"
            return None
        self.left -= 1
        return BLOCK


class Counting:
    def __init__(self, station=None, after=None, then=None):
        self.blocks, self.station, self.after, self.then = 0, station, after, then

    def start(self): ...
    def stop(self): ...

    def write(self, block):
        self.blocks += 1
        if self.after and self.blocks == self.after:
            self.then()


def test_a_station_plays_until_the_source_changes(tmp_path: Path, monkeypatch) -> None:
    FakeStream.made = []
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=10_000))
    st = _station(tmp_path)
    st.tts = None
    tuned = {"name": "Groove Salad", "url": "http://soma/gs"}
    st.tune(tuned)
    assert st.radio_tuned == {**tuned, "info": ""}
    st.output = Counting(after=50, then=lambda: st.tune(None))
    st._switch.clear()
    st._run_radio(st.radio_tuned)
    assert st.output.blocks == 50 and st.music_started
    status = st.status()
    assert status["radio"] is None and not status["can_skip"]
    assert any("Now - This" in h["text"] for h in st.history)


def test_status_while_a_station_plays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=10_000))
    st = _station(tmp_path)
    st.tts = None
    st.tune({"name": "RP", "url": "http://rp/"})
    seen = {}

    def look():
        seen.update(st.status())
        st.tune(None)
    st.output = Counting(after=20, then=look)
    st._switch.clear()
    monkeypatch.setattr(st, "_thread", type("T", (), {"is_alive": lambda self: True})())
    st._run_radio(st.radio_tuned)
    assert seen["radio"]["name"] == "RP" and seen["radio"]["playing"] and seen["radio"]["title"] == "Now - This"
    assert seen["now"]["kind"] == "radio" and seen["now"]["title"] == "Now - This" and seen["now"]["artist"] == "RP"
    assert seen["next"] is None and not seen["can_skip"] and not st.skip()


def test_a_dropped_stream_reconnects(tmp_path: Path, monkeypatch) -> None:
    FakeStream.made = []
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=20))
    monkeypatch.setattr(station_mod, "RADIO_RETRY_S", 0.2)
    st = _station(tmp_path)
    st.tune({"name": "RP", "url": "http://rp/"})
    out = st.output = Counting()
    out.write = lambda block: len(FakeStream.made) >= 3 and st.tune(None)
    st._switch.clear()
    st._run_radio(st.radio_tuned)
    assert len(FakeStream.made) == 3 and st.radio_error is None


def test_a_silent_station_gives_way_to_the_show(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=0))
    monkeypatch.setattr(station_mod, "RADIO_RETRY_S", 0.05)
    monkeypatch.setattr(station_mod, "RADIO_GIVE_UP_S", 0.5)
    st = _station(tmp_path)
    st.tune({"name": "Gone FM", "url": "http://gone/"})
    st.output = Counting()
    st._switch.clear()
    st._run_radio(st.radio_tuned)
    assert st.radio_tuned is None
    assert st.radio_error.startswith("Gone FM: ")
    assert not st.music_started


def test_tuning_in_cuts_the_show_short_and_back_again(tmp_path: Path, monkeypatch) -> None:
    """The whole show thread: music, a station, then music again."""
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=10_000))
    monkeypatch.setattr(pcm, "decode", lambda *a, **k: (np.zeros((4410, 2), np.int16) for _ in range(3000)))
    st = _station(tmp_path)
    st.tts, st._opening = None, None        # no DJ: straight into the music
    monkeypatch.setattr(st, "_scan", lambda path: _done(pcm.TrackScan(1.0, 0, 0, 300_000)))
    kinds = []

    def step():
        kinds.append(st.on_air.kind if st.on_air else None)
        n = st.output.blocks
        if n == 30:
            st.tune({"name": "RP", "url": "http://rp/"})
        elif n == 80:
            st.tune(None)
        elif n == 130:
            st._stop.set()
    out = Counting()
    out.write = lambda block: (setattr(out, "blocks", out.blocks + 1), step())
    st.output = out
    st._run_show()
    assert "track" in kinds[:30] and "radio" in kinds[30:80] and "track" in kinds[80:]
    assert st.current_track is not None


def _done(value):
    from concurrent.futures import Future
    f = Future()
    f.set_result(value)
    return f


def test_the_radio_resumes_its_station_at_start_up(tmp_path: Path) -> None:
    from dataclasses import asdict
    from sleepradiopi.config.settings import Settings
    from test_offline import FakeTts, NullOutput
    (tmp_path / "music").mkdir()
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "none", hooks_file="",
               scan_cache=tmp_path / "scans.json", tag_cache=None,
               radio_tuned={"name": "RP", "url": "http://rp/"})
    st = station_mod.Station(cfg, FakeTts(), NullOutput())
    assert st.radio_tuned["name"] == "RP"
    cfg["radio_tuned"] = {"name": "Bad", "url": "nope"}
    assert station_mod.Station(cfg, FakeTts(), NullOutput()).radio_tuned is None


# --- the web page's API ----------------------------------------------------------------------

class FakeStation:
    def __init__(self):
        self.tuned = None

    def tune(self, s):
        self.tuned = s

    def status(self):
        return {"on_air": True, "radio": self.tuned, "radio_error": None}


class FakeSpeaker:
    played = 0

    def play(self):
        FakeSpeaker.played += 1

    def status(self):
        return {}


def test_web_api(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    st, spk = FakeStation(), FakeSpeaker()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, spk, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, reply = call("/api/radio")
        assert code == 200 and reply["stations"] == radio.DEFAULT_STATIONS and reply["radio"] is None

        code, reply = call("/api/radio/play", {"name": "RP", "url": "http://rp/"})
        assert code == 200 and st.tuned["name"] == "RP" and FakeSpeaker.played == 1
        assert load(conf).radio_tuned == {"name": "RP", "url": "http://rp/", "info": ""}

        code, reply = call("/api/radio/play", {"name": "x", "url": "file:///etc/passwd"})
        assert code == 400 and "http" in reply["error"]

        code, reply = call("/api/radio/stations", {"stations": [{"name": "Mine", "url": "https://m/"}]})
        assert code == 200 and reply["stations"] == [{"name": "Mine", "url": "https://m/", "info": ""}]
        assert load(conf).radio_stations == reply["stations"]
        code, reply = call("/api/radio/stations", {"stations": []})
        assert reply["stations"] == []                         # an empty list stays empty

        code, reply = call("/api/radio/stop", {})
        assert code == 200 and st.tuned is None and load(conf).radio_tuned is None

        monkeypatch.setattr(radio, "search", lambda q: [{"name": q, "url": "http://q/", "info": ""}])
        code, reply = call("/api/radio/search?q=jazz%20fm")
        assert code == 200 and reply["results"][0]["name"] == "jazz fm"

        def offline(q):
            raise radio.StreamError("couldn't reach the station directory (is the radio online?)")
        monkeypatch.setattr(radio, "search", offline)
        code, reply = call("/api/radio/search?q=x")
        assert code == 400 and "online" in reply["error"]
    finally:
        httpd.shutdown()


def test_backups_keep_the_stations_but_not_whats_on(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"radio_stations": [{"name": "A", "url": "http://a/", "info": ""}],
                                "radio_tuned": {"name": "A", "url": "http://a/"}}))
    saved = backup.export(conf, 50)
    assert saved["settings"]["radio_stations"][0]["name"] == "A"
    assert "radio_tuned" not in saved["settings"]
    settings, _ = backup.parse(saved)
    assert settings["radio_stations"][0]["url"] == "http://a/"
    saved["settings"]["radio_stations"] = [{"name": "B", "url": "gopher://b/"}]
    with pytest.raises(backup.BadSettings, match="radio_stations"):
        backup.parse(saved)


def test_an_opening_cut_short_is_kept_for_coming_back(tmp_path: Path, monkeypatch) -> None:
    """Tuning in while the welcome is still being made (slow on a Zero) keeps
    it, half made, for when the show comes back, instead of starting again."""
    from concurrent.futures import Future
    from sleepradiopi.broadcast.station import Speech, Step
    monkeypatch.setattr(radio, "RadioStream", lambda url: FakeStream(url, blocks=10_000))
    st = _station(tmp_path)
    welcome = Speech("Good evening, and welcome", "v", Future())     # never finishes here
    first = st.tracks[0]
    st._opening = (st.builder.welcome_greeting(), [Step("say", welcome)], first)
    out = Counting()

    def write(block):
        out.blocks += 1
        if out.blocks == 5:
            st.tune({"name": "RP", "url": "http://rp/"})
        elif out.blocks == 40:
            st._stop.set()
    out.write = write
    st.output = out
    st._run_show()
    assert st._opening[1][0].speech is welcome and st._opening[2] is first
