import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.io.announce import Announcer, Clip, addresses, announcement, spoken_ip
from sleepradiopi.io.knob import EV_KEY, EVENT, KEY_DOWN, KEY_UP, PressTimer, handle
from sleepradiopi.web.server import make_handler


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_address_is_read_digit_by_digit() -> None:
    assert spoken_ip("192.168.1.42") == "one nine two, dot, one six eight, dot, one, dot, four two"
    text = announcement([("wlan0", "10.0.0.7")], "sleepradiopi")
    assert "one zero, dot, zero, dot, zero, dot, seven" in text
    assert text.count("one zero, dot") == 2 and "sleep radio pi dot local" in text
    assert "not connected" in announcement([], "sleepradiopi")


def test_addresses_skips_loopback() -> None:
    found = addresses()
    assert all(name != "lo" and not ip.startswith("127.") for name, ip in found)


def test_press_timer_tells_short_from_long() -> None:
    calls = []
    t = PressTimer(lambda: calls.append("short"), lambda: calls.append("long"), long_s=0.2)
    t.down(); t.up()
    assert calls == ["short"]
    t.down()
    assert _wait(lambda: calls == ["short", "long"])
    t.up()                                   # the release after a long press does nothing
    time.sleep(0.05)
    assert calls == ["short", "long"]


def test_handle_passes_key_releases() -> None:
    events = []
    data = EVENT.pack(0, 0, EV_KEY, 164, KEY_DOWN) + EVENT.pack(0, 0, EV_KEY, 164, 2) + \
        EVENT.pack(0, 0, EV_KEY, 164, KEY_UP)
    handle(data, lambda v: None, lambda: events.append("down"), lambda: events.append("up"))
    assert events == ["down", "up"]          # the auto-repeat (2) is ignored


def test_announcer_beeps_then_speaks_and_caches() -> None:
    played, rendered = [], []

    def render(text):
        rendered.append(text)
        return np.full((4410, 2), 5, dtype=np.int16)

    def play(clip):
        played.append(clip)
        clip.pos = clip.total                # as if the speaker played it at once
    a = Announcer(render, play, get_addresses=lambda: [("wlan0", "192.168.1.9")], host="sleepradiopi")
    assert a.speak()
    assert _wait(lambda: len(played) == 2)
    assert [c.kind for c in played] == ["beep", "announce"] and len(rendered) == 1
    assert _wait(lambda: a.speak())          # the second time uses the cached speech
    assert _wait(lambda: len(played) == 4) and len(rendered) == 1


def test_play_clip_while_paused_pauses_again_after(tmp_path: Path) -> None:
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    calls = []
    ctl = SpeakerControl(spk, lambda: calls.append("join"), lambda: calls.append("leave"))
    spk.start()
    assert ctl.paused
    clip = Clip(np.ones((2048, 2), dtype=np.int16), "announce", "Saying the address")
    ctl.play_clip(clip)
    assert not ctl.paused and spk.test is clip
    while spk.test is not None:
        spk.write(np.zeros((1024, 2), dtype=np.int16))
    assert _wait(lambda: ctl.paused and calls == ["join", "leave"], timeout=2)
    # while playing, a clip leaves it playing
    ctl.play()
    ctl.play_clip(Clip(np.ones((1024, 2), dtype=np.int16), "beep", "x"))
    spk.write(np.zeros((1024, 2), dtype=np.int16))
    time.sleep(0.7)
    assert not ctl.paused


def test_web_knob_button(tmp_path: Path) -> None:
    class FakeStation:
        def status(self):
            return {"on_air": True}

    class FakeAnnouncer:
        n = 0

        def speak(self):
            self.n += 1
            return True
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    ann = FakeAnnouncer()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl, None, ann))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def press(kind):
        req = urllib.request.Request(base + "/api/knob", data=json.dumps({"press": kind}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    try:
        assert press("short")["playing"] is True     # was paused
        assert press("short")["playing"] is False
        assert press("long")["done"] and ann.n == 1
    finally:
        httpd.shutdown()
