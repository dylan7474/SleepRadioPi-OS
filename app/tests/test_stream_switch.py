import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config.settings import load
from sleepradiopi.web import stream as stream_mod
from sleepradiopi.web.server import make_handler
from sleepradiopi.web.stream import Mp3Output

SECOND = np.zeros((44_100, 2), dtype=np.int16)


def test_off_it_still_paces_the_show_but_runs_no_encoder(monkeypatch) -> None:
    monkeypatch.setattr(stream_mod, "LEAD_S", 0.0)
    out = Mp3Output(enabled=False)
    out.start()
    t0 = time.monotonic()
    out.write(SECOND[:22_050])
    out.write(SECOND[:22_050])                 # the second half-second has to wait
    assert time.monotonic() - t0 > 0.4
    assert out._proc is None
    out.stop()


def test_switched_on_and_off_mid_show() -> None:
    out = Mp3Output(enabled=False)
    out.start()
    out.set_enabled(True)
    assert out._proc is not None               # the encoder starts at once
    listener = out.add_client()
    out.set_enabled(False)
    assert out._proc is None and listener.get(timeout=1) is None   # listeners are told to go
    out.stop()


class FakeStation:
    def status(self):
        return {"on_air": True}


def test_web_switch(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    out = Mp3Output(enabled=False)
    ctl = SpeakerControl(SpeakerOutput(command=["sh", "-c", "cat > /dev/null"]), lambda: None, lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), out, ctl, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        r = urllib.request.Request(base + "/api/stream", data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        with urllib.request.urlopen(base + "/api/status") as r:
            assert json.load(r)["stream"] is False
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/stream")
        assert err.value.code == 404
        assert post({"enabled": True}) == {"stream": True} and out.enabled
        assert load(conf).web_stream is True
        assert post({"enabled": False}) == {"stream": False} and load(conf).web_stream is False
        with pytest.raises(urllib.error.HTTPError):
            post({"enabled": "yes"})
    finally:
        httpd.shutdown()


def test_without_a_speaker_it_cant_be_switched_off(tmp_path: Path) -> None:
    out = Mp3Output(enabled=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), out, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/stream",
                                   data=b'{"enabled": false}', method="POST",
                                   headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(r)
        assert err.value.code == 400 and out.enabled
    finally:
        httpd.shutdown()


def test_back_to_the_show_or_power_on_plays_a_short_jingle_rather_than_wait(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    st.config.jingle_every = 4
    short, long_ = JingleClip(tmp_path / "Short.mp3", 31.0), JingleClip(tmp_path / "Long.mp3", 65.0)
    st.jingles = [short, long_]
    pending = Future()
    welcome = station_mod.Step("say", station_mod.Speech("Good evening", "stock", pending))
    steps = st._opening_steps([welcome], "back to the show")
    assert [(s.kind, s.jingle) for s in steps] == [("jingle", short)]     # a short one first, of its own
    assert pending.cancelled()                                            # and the welcome isn't made after all
    st.jingles = [long_]
    hi = lambda: [station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))]
    assert st._opening_steps(hi(), "back to the show")[0].jingle == long_   # its own, even a long one (before Default's)
    assert st._opening_steps(hi(), "power-on")[0].jingle == long_           # power-on: the station's own, any
    st.jingles, st.config.jingle_every = [short], 0                       # jingles off: straight to the music
    assert st._opening_steps([station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))], "power-on") == []
    done = Future()
    done.set_result(None)
    ready = [station_mod.Step("say", station_mod.Speech("Good evening", "stock", done))]
    assert st._opening_steps(ready, "power-on") is ready                  # made in time: said as usual


def test_a_gap_never_waits_for_the_dj(tmp_path, monkeypatch) -> None:
    """Like a real station: a line not made by the song's end is skipped, a short
    jingle fills in (once), and a message comes round again at the next gap."""
    from concurrent.futures import Future
    from datetime import datetime
    import numpy as np
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(station_mod, "GAP_GRACE_S", 0.05)
    monkeypatch.setattr(st, "_write", lambda block: None)
    played, said = [], []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air: played.append(path.stem))
    monkeypatch.setattr(st, "_speak", lambda speech, kind="dj": said.append(speech.text) or True)
    st.config.jingle_every = 4
    st.jingles = [JingleClip(tmp_path / "Short.mp3", 31.0), JingleClip(tmp_path / "Long.mp3", 65.0)]
    st._tracks_since_jingle = 2
    st.messages.set({"list": [{"text": "Love from home"}], "date_first": False})
    at = datetime(2026, 10, 1, 11, 27)
    assert st.messages.due(at, True) == "Love from home"
    record = st.messages.played(at)
    ready = Future()
    ready.set_result(np.zeros((10, 2), dtype=np.int16))
    steps = [station_mod.Step("say", station_mod.Speech("Love from home", "stock", Future()), message=record),
             station_mod.Step("say", station_mod.Speech("That was a song", "stock", Future())),
             station_mod.Step("say", station_mod.Speech("Here's the next", "stock", ready))]
    st._run_steps(steps, gap=True)
    assert played == ["Short"] and said == ["Here's the next"]           # one jingle, then what was ready
    assert st.messages.due(at, True) == "Love from home"                  # the message: at the next gap
    played.clear()
    st._run_steps([station_mod.Step("jingle", jingle=st.jingles[1]),
                   station_mod.Step("say", station_mod.Speech("Not yet", "stock", Future()))], gap=True)
    assert played == ["Long"]                                              # the gap had its jingle: no second
    played.clear()
    st.config.jingle_every = 0
    st._run_steps([station_mod.Step("say", station_mod.Speech("Not yet", "stock", Future()))], gap=True)
    assert played == []                                                    # jingles off: on to the music



def test_a_gap_fills_with_a_jingle_then_says_what_got_ready(tmp_path, monkeypatch) -> None:
    """A line not made by the song's end: a jingle (the theme's short one, else
    Default's) while it's made; then it's said if it's ready, skipped if not."""
    from concurrent.futures import Future
    import numpy as np
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(station_mod, "GAP_GRACE_S", 0.05)
    monkeypatch.setattr(st, "_write", lambda block: None)
    later = Future()
    played, said = [], []
    def play(path, on_air):
        played.append(path.stem)
        later.set_result(np.zeros((10, 2), dtype=np.int16))      # made while the jingle played
    monkeypatch.setattr(st, "_play_file", play)
    monkeypatch.setattr(st, "_speak", lambda speech, kind="dj": said.append(speech.text) or True)
    st.config.jingle_every, st.jingles = 4, []                    # (jingles on, none of its own: Default's fill in)
    st._tracks_since_jingle = 2
    st._default_jingles = [JingleClip(tmp_path / "Start.mp3", 20.0)]
    st._run_steps([station_mod.Step("say", station_mod.Speech("Here's one", "stock", later))], gap=True)
    assert played == ["Start"] and said == ["Here's one"]
    assert st._tracks_since_jingle == 0                           # (it was the jingle: "every 4" counts from it)


def _gap_station(tmp_path, monkeypatch):
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(station_mod, "GAP_GRACE_S", 0.05)
    monkeypatch.setattr(st, "_write", lambda block: None)
    played, said = [], []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air: played.append(path.stem))
    monkeypatch.setattr(st, "_speak", lambda speech, kind="dj": said.append(speech.text) or True)
    st.config.jingle_every, st.jingles = 4, [JingleClip(tmp_path / "Own.mp3", 20.0)]
    st._tracks_since_jingle = 2
    return st, played, said


def test_a_line_that_failed_is_dropped_with_no_jingle(tmp_path, monkeypatch) -> None:
    """The voice failed (it was killed for memory): nothing to wait for, so no
    jingle for it -- straight on (it was a jingle in every gap)."""
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    st, played, said = _gap_station(tmp_path, monkeypatch)
    failed = Future()
    failed.set_exception(BrokenPipeError(32, "Broken pipe"))
    st._run_steps([station_mod.Step("say", station_mod.Speech("Here's one", "stock", failed))], gap=True)
    assert played == [] and said == []


def test_no_fill_jingle_two_gaps_running_or_with_jingles_off(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    st, played, said = _gap_station(tmp_path, monkeypatch)
    slow = lambda: [station_mod.Step("say", station_mod.Speech("Here's one", "stock", Future()))]
    st._run_steps(slow(), gap=True)
    assert played == ["Own"]
    st._tracks_since_jingle = 1                                   # (the next song started: the next gap)
    st._run_steps(slow(), gap=True)
    assert played == ["Own"]                                      # not again: straight on
    st._tracks_since_jingle, st.config.jingle_every = 3, 0        # jingles off: none, not even Default's
    st._run_steps(slow(), gap=True)
    assert played == ["Own"] and said == []


def test_the_news_never_waits_in_silence_for_its_time_line(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future
    from types import SimpleNamespace
    import numpy as np
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(station_mod, "GAP_GRACE_S", 0.05)
    monkeypatch.setattr(st, "_write", lambda block: None)
    played, said = [], []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air: played.append(path.stem))
    monkeypatch.setattr(st, "_speak", lambda speech, kind="dj": said.append(speech.text) or True)
    monkeypatch.setattr(st.news_repo, "mark_read", lambda headlines: None)
    st._default_jingles = [JingleClip(tmp_path / "Start.mp3", 20.0)]
    st.config.jingle_every, st.jingles, st._tracks_since_jingle = 4, [], 2
    body = Future()
    body.set_result(np.zeros((10, 2), dtype=np.int16))
    news = SimpleNamespace(time_line=station_mod.Speech("It's two o'clock", "stock", Future()),
                           body=station_mod.Speech("The stories", "stock", body), headlines=[], due=None)
    st._run_steps([station_mod.Step("news", news=news)], gap=True)
    assert played == ["Start"] and said == ["The stories"]        # a jingle, then straight to the stories
