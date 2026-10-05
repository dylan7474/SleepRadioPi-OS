"""Listen here: the radio heard in a browser instead of from its own speaker (web/hear.py)."""
import time

import numpy as np

from sleepradiopi.audio.speaker import SpeakerControl
from sleepradiopi.web.hear import HearHere


class Stream:
    def __init__(self, enabled):
        self.enabled, self.n, self.calls, self.dropped = enabled, 0, [], 0

    def set_enabled(self, on):
        self.enabled = on
        self.calls.append(on)

    def listeners(self):
        return self.n

    def drop_listeners(self):
        self.dropped += 1
        self.n = 0


class Card:                                          # the sound card, as SpeakerControl uses it
    def __init__(self):
        self.volume, self.enabled, self.noise, self.noise_mix, self.mono, self.eq, self.fade_end, self.test = 30, False, None, 50, False, None, None, None

    def set_enabled(self, on):
        self.enabled = on

    def set_noise(self, gen):
        self.noise = gen

    def play_test(self, sound):
        self.test = sound


def _radio(playing=True, stream=False, gone_s=60.0, clock=time.monotonic):
    card, joins = Card(), []
    ctl = SpeakerControl(card, join=lambda: joins.append("join"), leave=lambda: joins.append("leave"))
    if playing:
        ctl.play()
    out = Stream(enabled=stream)
    h = HearHere(out, ctl, clock=clock, gone_s=gone_s, look_s=0.01)
    ctl.elsewhere = h
    return ctl, card, out, h


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_sound_goes_to_the_browser_and_comes_back() -> None:
    ctl, card, out, h = _radio()
    h.hush()                                              # (nothing to do unless it's on)
    assert card.enabled and not h.on
    h.start()
    assert h.on and out.enabled and card.enabled          # the stream is made first: the speaker still plays
    out.n = 1
    h.hush()
    assert not card.enabled and not ctl.paused            # heard there: the speaker quiet, and the radio is NOT paused
    assert ctl.status()["playing"] is True
    h.stop()
    assert not h.on and card.enabled and not ctl.paused and not out.enabled     # back, playing; the stream off as it was

    ctl, card, out, h = _radio(playing=False, stream=True)    # the radio paused, the stream on anyway
    h.start()
    out.n = 1
    h.hush()
    assert not ctl.paused and not card.enabled            # listened to there: it plays (there)
    h.stop()
    assert card.enabled and not ctl.paused and out.calls == [] and out.enabled   # ...and carries on from the speaker; the stream left on

    out = Stream(enabled=False)                           # a radio with no speaker: nothing to quieten
    h = HearHere(out, None, gone_s=60, look_s=0.01)
    h.start()
    h.hush()
    h.hold()
    h.resume()
    h.stop()
    assert not h.on and not out.enabled


def test_pause_and_play_are_about_the_listening_there_and_the_speaker_stays_quiet() -> None:
    ctl, card, out, h = _radio()
    h.start()
    out.n = 1
    h.hush()
    ctl.toggle()                                          # the knob (or the page, or a remote): pause
    assert ctl.paused and h.held and not card.enabled and out.dropped == 1 and out.n == 0    # the browser let go; no speaker
    assert ctl.status()["playing"] is False
    ctl.toggle()                                          # again: play -- there, not here
    assert not ctl.paused and not h.held and not card.enabled
    ctl.pause()                                           # (the sleep timer ending, say)
    ctl.pause()
    assert h.held and out.dropped == 2 and not card.enabled
    ctl.play()                                            # (a preset button plays)
    assert not h.held and not card.enabled
    ctl.pause()
    h.stop()                                              # Listen here off while paused: the speaker's, and still paused
    assert not h.on and ctl.paused and not card.enabled
    ctl.toggle()                                          # ...and now the knob is the speaker's again
    assert card.enabled and not ctl.paused


def test_a_browser_that_just_goes_gives_the_speaker_back() -> None:
    now = [100.0]
    ctl, card, out, h = _radio(gone_s=20, clock=lambda: now[0])
    h.start()
    out.n = 1
    h.hush()
    now[0] += 500                                          # tuned in the whole time: left alone
    time.sleep(0.1)
    assert h.on and not card.enabled
    out.n = 0                                              # the lid shut
    time.sleep(0.1)
    now[0] += 10
    time.sleep(0.1)
    assert h.on                                            # (a moment's gap isn't going away)
    now[0] += 15
    assert _wait(lambda: not h.on) and card.enabled and not ctl.paused and not out.enabled

    ctl, card, out, h = _radio(gone_s=20, clock=lambda: now[0])    # paused there, the page still saying it's there: kept
    h.start()
    out.n = 1
    h.hush()
    ctl.pause()
    for _ in range(4):
        now[0] += 15
        h.start()                                          # ("still here")
        time.sleep(0.05)
    assert h.on and h.held
    now[0] += 25                                           # ...then it stops saying so: the radio's again, still paused
    assert _wait(lambda: not h.on) and ctl.paused and not card.enabled

    ctl, card, out, h = _radio(gone_s=20, clock=lambda: now[0])    # never tuned in at all (it wouldn't play): given back too
    h.start()
    now[0] += 25
    assert _wait(lambda: not h.on) and card.enabled and not out.enabled
