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


def test_back_to_the_show_plays_a_short_jingle_rather_than_wait_for_the_welcome(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi.broadcast.library import JingleClip
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(type(st), "main_mix", property(lambda self: False))   # a theme: still a jingle
    st.config.jingle_every = 4
    short, long_ = JingleClip(tmp_path / "Short.mp3", 31.0), JingleClip(tmp_path / "Long.mp3", 65.0)
    st.jingles = [short, long_]
    pending = Future()
    welcome = station_mod.Step("say", station_mod.Speech("Good evening", "stock", pending))
    steps = st._back_steps([welcome])
    assert [(s.kind, s.jingle) for s in steps] == [("jingle", short)]     # only a short one, never the long
    assert pending.cancelled()                                            # and the welcome isn't made after all
    st.jingles = [long_]
    assert st._back_steps([station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))]) == []
    st.jingles, st.config.jingle_every = [short], 0                       # jingles off: straight to the music
    assert st._back_steps([station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))]) == []
    done = Future()
    done.set_result(None)
    ready = [station_mod.Step("say", station_mod.Speech("Good evening", "stock", done))]
    assert st._back_steps(ready) is ready                                 # made in time: said as usual


def test_power_on_ticks_until_the_welcome_is_ready(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    from sleepradiopi import startup_sound
    from test_offline import _station
    st = _station(tmp_path)
    monkeypatch.setattr(startup_sound, "settings", lambda home: (True, "stock", 50))
    monkeypatch.setattr(station_mod, "SPEECH_WAIT_S", 30.0)
    pending = Future()
    speech = station_mod.Speech("Good evening", "stock", pending)
    written = []

    def write(block):
        written.append(len(block))
        if sum(written) > 3 * 44100 * (station_mod.WARM_TICK_S + 0.04):
            pending.set_result(None) if not pending.done() else None
    monkeypatch.setattr(st, "_write", write)
    st._tick_until_ready([station_mod.Step("say", speech)])
    assert pending.done() and sum(written) > 3 * 44100 * station_mod.WARM_TICK_S
    tick = station_mod._tick()
    assert len(tick) == int(0.04 * 44100) and 0 < abs(tick).max() < 0.15 * 32767
    written.clear()
    st._tick_until_ready([station_mod.Step("say", speech)])
    assert written == []                                          # ready: straight on

