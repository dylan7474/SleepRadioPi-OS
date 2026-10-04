"""The plain voice (eSpeak) standing in where the radio must say something
before there's a DJ's voice: notices, and the first voice's download."""
import time

import numpy as np

from sleepradiopi import main


class FakeControl:
    def __init__(self):
        self.played = []

    def play_clip(self, clip):
        self.played.append(clip)


class FakeStation:
    def __init__(self, has_voice):
        self._has_voice = has_voice
        self.rendered = []

    def render_speech(self, text):
        self.rendered.append(text)
        return np.full((4410, 2), 5, dtype=np.int16)


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_notices_use_the_plain_voice_until_there_is_a_djs(monkeypatch) -> None:
    said = []
    monkeypatch.setattr(main.plain_voice, "render",
                        lambda text: (said.append(text), np.full((22050, 2), 7, dtype=np.int16))[1])
    control, new = FakeControl(), FakeStation(has_voice=False)
    main._say_now(new, control, "Downloading an update.")              # a new radio: no DJ's voice yet
    assert said == ["Downloading an update."] and new.rendered == []
    assert control.played[0].kind == "notice" and control.played[0].audio[0, 0] == 7

    control, old = FakeControl(), FakeStation(has_voice=True)
    main._say_now(old, control, "Downloading an update.")              # the DJ says it once he's there
    assert _wait(lambda: len(control.played) == 1) and old.rendered == ["Downloading an update."]
    assert len(said) == 1 and control.played[0].audio[0, 0] == 5

    main._say_now(new, None, "No speaker.")                            # nothing to say it on: only logged
    assert len(said) == 1
    assert main._say_plainly(FakeControl(), "Half a second.") == 0.5   # (how long it takes to say)
    monkeypatch.setattr(main.plain_voice, "render", lambda text: None) # no eSpeak here (not the image)
    control = FakeControl()
    assert main._say_plainly(control, "Nothing.") == 0.0 and control.played == []


def test_the_first_voice_download_is_announced_once_online(monkeypatch) -> None:
    said = []
    monkeypatch.setattr(main.plain_voice, "render",
                        lambda text: (said.append(text), np.zeros((4410, 2), dtype=np.int16))[1])
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    class Jobs:
        tries = 0

        def download_standard(self, wait=False):
            self.tries += 1

        def status(self):
            return {"state": "done" if self.tries >= 4 else "failed"}
    jobs, online = Jobs(), iter([False, False, True, True])
    main._fetch_standard_voice(jobs, FakeControl(), online=lambda: next(online))
    assert jobs.tries == 4 and said == [main.NO_VOICE_YET]             # not while it's still a hotspot; then once
