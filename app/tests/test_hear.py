"""Listen here: the radio heard in a browser instead of from its own speaker (web/hear.py)."""
import time

from sleepradiopi.web.hear import HearHere


class Stream:
    def __init__(self, enabled):
        self.enabled, self.n, self.calls = enabled, 0, []

    def set_enabled(self, on):
        self.enabled = on
        self.calls.append(on)

    def listeners(self):
        return self.n


class Speaker:
    def __init__(self, paused=False):
        self.paused, self.calls = paused, []

    def pause(self):
        self.paused = True
        self.calls.append("pause")

    def play(self):
        self.paused = False
        self.calls.append("play")


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_sound_goes_to_the_browser_and_comes_back() -> None:
    out, sp = Stream(enabled=False), Speaker()
    h = HearHere(out, sp, gone_s=60, look_s=0.01)
    h.hush()                                              # (nothing to do unless it's on)
    assert sp.calls == [] and not h.on
    h.start()
    assert h.on and out.enabled and sp.calls == []        # the stream is made first: the speaker still plays
    out.n = 1
    h.hush()
    h.hush()
    assert sp.calls == ["pause"]                          # heard there: quiet here (once)
    h.stop()
    assert not h.on and sp.calls == ["pause", "play"] and not out.enabled      # back, and the stream off as it was
    h.stop()
    assert sp.calls == ["pause", "play"]

    out, sp = Stream(enabled=True), Speaker(paused=True)  # the stream was on anyway, the radio paused: both left as they were
    h = HearHere(out, sp, gone_s=60, look_s=0.01)
    h.start()
    out.n = 1
    h.hush()
    h.stop()
    assert out.calls == [] and sp.calls == [] and out.enabled and sp.paused

    h = HearHere(Stream(enabled=False), None, gone_s=60, look_s=0.01)   # a radio with no speaker: nothing to quieten
    h.start()
    h.hush()
    h.stop()
    assert not h.on


def test_a_browser_that_just_goes_gives_the_speaker_back() -> None:
    now = [100.0]
    out, sp = Stream(enabled=False), Speaker()
    h = HearHere(out, sp, clock=lambda: now[0], gone_s=20, look_s=0.01)
    h.start()
    out.n = 1
    h.hush()
    now[0] += 500                                          # tuned in the whole time: left alone
    time.sleep(0.1)
    assert h.on and sp.paused
    out.n = 0                                              # the lid shut
    time.sleep(0.1)
    now[0] += 10
    time.sleep(0.1)
    assert h.on                                            # (a moment's gap isn't going away)
    now[0] += 15
    assert _wait(lambda: not h.on) and sp.calls == ["pause", "play"] and not out.enabled

    out, sp = Stream(enabled=False), Speaker()             # never tuned in at all (it wouldn't play): given back too
    h = HearHere(out, sp, clock=lambda: now[0], gone_s=20, look_s=0.01)
    h.start()
    now[0] += 25
    assert _wait(lambda: not h.on) and sp.calls == [] and not out.enabled
