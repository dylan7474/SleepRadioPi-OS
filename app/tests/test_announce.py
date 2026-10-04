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


def test_set_up_lines_are_said_in_the_plain_voice(tmp_path: Path) -> None:
    from sleepradiopi.io import announce
    from sleepradiopi.tts import plain
    played, rendered, plainly = [], [], []

    def render(text):
        rendered.append(text)
        return np.full((4410, 2), 5, dtype=np.int16)

    def say_plainly(text):
        plainly.append(text)
        return np.full((2205, 2), 7, dtype=np.int16)

    def play(clip):
        played.append(clip)
        clip.pos = clip.total
    a = Announcer(render, play, get_addresses=lambda: [("wlan0", "192.168.1.9")], host="sleepradiopi", plain=say_plainly)
    assert a.say([announce.joined_text("Home"), a.text], announce.cue("joined"), plain=True)
    assert _wait(lambda: len(played) == 3) and rendered == []           # the DJ's voice isn't waited for
    assert plainly == ["I'm on Home.", a.text()] and played[1].label == "Wi-Fi" and played[1].audio[-1, 0] == 7
    assert a.say([announce.JOINING], announce.cue("joining"), wait=3, plain=True)   # (waits its turn, isn't dropped)
    assert _wait(lambda: not a._busy.locked())

    played.clear()                           # the long press stays in the DJ's voice ...
    assert a.speak() and _wait(lambda: len(played) == 2) and rendered == [a.text()]
    assert _wait(lambda: not a._busy.locked())
    b = Announcer(None, play, get_addresses=lambda: [], host="sleepradiopi", plain=say_plainly)
    played.clear()                           # ... unless there isn't one (a new radio): the plain one
    assert b.speak() and _wait(lambda: len(played) == 2) and plainly[-1] == "I'm not connected to a network."
    assert _wait(lambda: not b._busy.locked())

    c = Announcer(render, play, host="sleepradiopi")                     # no eSpeak (not the image): the DJ's
    played.clear()
    assert c.say([announce.JOINING], announce.cue("joining"), plain=True)
    assert _wait(lambda: len(played) == 2) and rendered[-1] == announce.JOINING
    d = Announcer(None, play, host="sleepradiopi")                       # neither: the tune alone says it
    played.clear()
    assert _wait(lambda: d.say([announce.JOINING], announce.cue("failed"), plain=True))
    assert _wait(lambda: not d._busy.locked()) and [c.kind for c in played] == ["beep"]

    assert "I couldn't join Home," in announce.failed_text({"ssid": "SleepRadio-Setup"}, "Home")
    for name in announce.CUES:
        assert announce.cue(name).shape[1] == 2 and 0.2 < len(announce.cue(name)) / 44100 < 1

    # eSpeak's own WAV (sizes not filled in when piped), brought to the speaker's rate and channels
    wav = b"RIFF\xff\xff\xff\x7fWAVEfmt \x10\0\0\0\x01\0\x01\0" + (22050).to_bytes(4, "little") + \
        (44100).to_bytes(4, "little") + b"\x02\0\x10\0data\xff\xff\xff\x7f" + \
        (np.sin(np.arange(2205) / 5) * 3000).astype("<i2").tobytes()
    audio = plain.from_wav(wav)
    assert audio.shape == (4410, 2) and audio.dtype == np.int16 and abs(int(np.abs(audio).max()) - plain.PEAK) < 50
    assert plain.from_wav(b"") is None and plain.from_wav(b"RIFF....WAVEfmt ") is None
    if plain.available():                    # (this computer has eSpeak: the real thing)
        spoken = plain.render("Joining your Wi-Fi now.")
        assert spoken is not None and 0.5 < len(spoken) / 44100 < 5


def test_play_clip_while_paused_leaves_the_show_paused(tmp_path: Path) -> None:
    """The time signal on a paused radio: it plays on its own -- the show isn't
    woken for it (that let a burst of music out after the pips)."""
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    calls = []
    ctl = SpeakerControl(spk, lambda: calls.append("join"), lambda: calls.append("leave"))
    spk.start()
    assert ctl.paused
    clip = Clip(np.ones((2048, 2), dtype=np.int16), "announce", "Saying the address")
    ctl.play_clip(clip)
    assert ctl.paused and not spk.enabled and calls == []
    assert _wait(lambda: spk.test is None and spk._proc is None, timeout=2)   # played by itself, the speaker off again
    assert ctl.paused and calls == []
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


def test_a_bounce_is_not_a_second_press() -> None:
    calls = []
    t = PressTimer(lambda: calls.append("short"), lambda: calls.append("long"), long_s=0.3, guard_s=0.2)
    t.down(); t.up()                       # the switch opens for a moment...
    t.down(); t.up()                       # ...and closes again: one press, not two
    assert calls == ["short"]
    time.sleep(0.25)
    t.down(); t.up()                       # a real second press, after the guard
    assert calls == ["short", "short"]
    t.down(); t.up()                       # a bounce...
    t.down(); time.sleep(0.4); t.up()      # ...then held: still a hold
    assert calls == ["short", "short", "long"]


def test_the_preset_buttons_are_guarded_and_the_knob_is_not() -> None:
    from sleepradiopi.io.knob import PRESS_GUARD_S, Knob
    calls = []
    k = Knob(lambda n: None, lambda: calls.append("knob"), on_long_press=lambda: None,
             buttons={2: (lambda: calls.append("b1"), lambda: None)})
    for _ in range(2):
        k.keys[2][0](); k.keys[2][1]()
        k.on_press(); k.on_release()
    assert calls == ["b1", "knob", "knob"] and PRESS_GUARD_S == 0.15
