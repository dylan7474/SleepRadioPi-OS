import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.playback import ebooks as eb
from sleepradiopi.playback import reader as rd
from sleepradiopi.web.server import make_handler

from test_offline import _station

TEXT = "CHAPTER I.\n\n" + "\n\n".join(" ".join(f"Sentence {i} of paragraph {k} is here, and Mr. Bennet agrees." for i in range(4)) for k in range(5)) \
    + "\n\nCHAPTER II.\n\nThe second chapter has one paragraph only. It ends here.\n"


class Voice:
    """A voice worker that makes 0.05 s of silence a character (instantly), and keeps the voice it was told to use."""
    ready = True
    last_rss_mb = 100
    restarts = 0

    def __init__(self, tmp_path):
        self.voice, self.idle_s, self.calls, self.said = "personal", 30.0, [], []
        self.voices_dir = tmp_path

    def use_voice(self, voice):
        self.calls.append(voice)
        self.voice = voice
        return True

    def synth(self, voice, text, speed):
        self.said.append(text)
        return np.zeros(int(len(text) * 0.05 * 16000), np.float32), 16000


class Out:
    def __init__(self):
        self.blocks = 0

    def start(self): ...
    def stop(self): ...

    def write(self, block):
        self.blocks += 1
        time.sleep(0.0005)


def fake_speech(samples, rate, volume):
    return np.zeros((int(len(samples) / rate * pcm.SAMPLE_RATE), pcm.CHANNELS), np.int16)


@pytest.fixture
def st(tmp_path, monkeypatch):
    s = _station(tmp_path)
    (tmp_path / "ebooks").mkdir()
    (tmp_path / "ebooks" / "small.txt").write_text(TEXT)
    s.ebooks = eb.EbookLibrary(tmp_path / "ebooks")
    s.ebooks.scan()
    s.tts = Voice(tmp_path)
    s.ebooks_on = True                         # (whatever the machine running the tests has)
    s.dj_voice = "personal"
    s.output = Out()
    real = rd.Reader
    monkeypatch.setattr(station_mod.reader_mod, "Reader",
                        lambda tts, voice, **kw: real(tts, voice, speech=fake_speech, target_lead_s=1000, start_lead_s=0.05, **kw))
    return s


KEY = eb.EBOOK + "small.txt"


def test_an_ebook_is_read_to_the_end_in_the_stock_voice_and_the_dj_voice_comes_back(st):
    ended = []
    st._listeners = 1
    st.on_book_end = lambda: (ended.append(True), st._stop.set())
    st.play_book(KEY)
    st._switch.clear()
    st._run_book(st.source)
    assert ended == [True]
    assert st.book_positions.get(KEY) == st.ebooks.get("small.txt").total_ms          # finished: the whole book
    assert st.tts.calls == ["stock", "personal"] and st.tts.idle_s == station_mod.TTS_IDLE_S
    assert "Mister Bennet agrees." in " ".join(st.tts.said) and "Mr." not in " ".join(st.tts.said)
    assert st.output.blocks > 50


def test_it_can_be_listed_sought_and_paused_where_it_is(st):
    assert [(b["key"], b["title"], b["chapters"]) for b in st.ebook_list()] == [(KEY, "small", 2)]
    st._listeners = 1
    st.on_book_end = lambda: st._stop.set()
    st.play_book(KEY)
    st._switch.clear()
    book = st._book(KEY)
    t = threading.Thread(target=st._run_book, args=(st.source,), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while st.book_now is None and time.monotonic() < end:
        time.sleep(0.01)
    target = book.ebook.units[10].start_ms + 3
    got = st.book_seek(to_ms=target)
    assert got["pos_ms"] == target and got["chapters"] == 2
    time.sleep(0.3)
    st._listeners = 0                                       # paused
    time.sleep(0.5)
    saved = st.book_positions.get(KEY)
    starts = [u.start_ms for u in book.ebook.units]
    assert saved in starts and saved >= book.ebook.units[10].start_ms         # the start of a sentence, not mid-word
    st._stop.set()
    t.join(5)
    assert not t.is_alive()


def test_the_api_lists_and_plays_ebooks_and_refuses_without_a_voice(tmp_path, st):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, d = call("/api/ebooks")
        assert code == 200 and d["can_read"] and d["ebooks"][0]["key"] == KEY
        code, d = call("/api/books/play", {"key": KEY})
        assert code == 200 and d["source"]["kind"] == "book" and d["source"]["key"] == KEY
        st.tts = None                                       # a radio with its voice off can't read
        code, d = call("/api/books/play", {"key": KEY})
        assert code == 400 and "no voice" in d["error"]
    finally:
        httpd.shutdown()


def test_the_reading_voice_speed_and_pauses_are_settings(st, tmp_path):
    (tmp_path / "voices" / "stock").mkdir(parents=True)
    (tmp_path / "voices" / "stock" / "model.onnx").write_bytes(b"x")
    (tmp_path / "voices" / "personal").mkdir()
    (tmp_path / "voices" / "personal" / "model.onnx").write_bytes(b"x")
    st.voices_dir = tmp_path / "voices"
    assert (st.config.ebook_voice, st.config.ebook_speed, st.config.ebook_pause) == ("stock", 0.85, 1.0)
    st.set_dj(ebook_voice="personal", ebook_speed=0.7, ebook_pause=1.5)
    assert (st.config.ebook_voice, st.config.ebook_speed, st.config.ebook_pause) == ("personal", 0.7, 1.5)
    for bad in ({"ebook_voice": "nobody"}, {"ebook_speed": 3}, {"ebook_pause": 0.1}, {"ebook_pause": True}):
        with pytest.raises(ValueError):
            st.set_dj(**bad)
    assert st.dj_settings()["ebook_speed"] == 0.7 if hasattr(st, "dj_settings") else True
    st.set_dj(ebook_voice="stock")
    seen = {}
    st._listeners = 1
    st.on_book_end = lambda: st._stop.set()
    st.play_book(KEY)
    st._switch.clear()
    st._run_book(st.source)
    assert st.tts.calls[0] == "stock"                                   # (it reads in the voice chosen)


def test_ebooks_are_on_by_memory_unless_the_setting_says_otherwise():
    assert eb.enabled("auto", 478_084) is False and eb.enabled("auto", 949_000) is True      # a Zero 2 W; a Pi 3
    assert eb.enabled("on", 478_084) is True and eb.enabled("off", 949_000) is False
    assert eb.enabled("auto", 0) is False                                                    # (memory unknown: leave it off)


def test_a_radio_without_the_memory_has_no_ebooks(st):
    st.ebooks_on = False
    assert st.status()["ebooks"] is False and st.ebook_list() == []
    with pytest.raises(ValueError, match="memory"):
        st.play_book(KEY)
    assert st._book(KEY) is None
    st.ebooks_on = True
    assert st.status()["ebooks"] is True and [b["key"] for b in st.ebook_list()] == [KEY]
