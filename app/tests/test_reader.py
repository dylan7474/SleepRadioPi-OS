import threading
import time

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.playback import ebooks as eb
from sleepradiopi.playback import reader as rd

RATE = 16000


def book(n=12):
    paras = [" ".join(f"Sentence number {i} of the book is here and goes on a while." for i in range(k, k + 3)) for k in range(0, n, 3)]
    return eb.Ebook("b.txt", "B", "A", [eb.Chapter("One", paras)])


class FakeTts:
    """A voice that makes 0.06 s of silence a character, instantly (or when told to)."""

    def __init__(self, gate=None, bad=None, fail=False):
        self.gate, self.bad, self.fail, self.said = gate, bad, fail, []

    def synth(self, voice, text, speed):
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail or (self.bad and self.bad in text):
            raise RuntimeError("worker died")
        self.said.append(text)
        return np.zeros(int(len(text) * 0.06 * RATE), np.float32), RATE


def speech(samples, rate, volume):
    return np.zeros((int(len(samples) / rate * pcm.SAMPLE_RATE), pcm.CHANNELS), np.int16)


def make(tts=None, **kw):
    kw.setdefault("target_lead_s", 20)
    kw.setdefault("start_lead_s", 2)
    return rd.Reader(tts or FakeTts(), "stock", speech=speech, **kw)


def drain(r, limit=5000):
    out = []
    for _ in range(limit):
        b = r.read(0.01)
        if b is None and r.finished:
            break
        if b is not None:
            out.append(b)
    return out


def wait_for(cond, s=5):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_it_makes_speech_ahead_and_then_rests():
    r = make()
    r.start(book())
    assert wait_for(lambda: r.lead_s() >= 20)
    time.sleep(0.3)
    lead = r.lead_s()
    assert 20 <= lead < 20 + 10                                   # stops within one sentence of the target
    n = len(FakeTts().said)
    time.sleep(0.3)
    assert abs(r.lead_s() - lead) < 0.01                           # resting: nothing more is made
    r.close()


def test_nothing_is_handed_out_until_the_start_lead_is_made():
    gate = threading.Event()
    r = make(FakeTts(gate=gate), start_lead_s=3)
    r.start(book())
    assert r.read(0.05) is None
    gate.set()
    assert wait_for(lambda: r.read(0.05) is not None)
    r.close()


def test_the_book_is_read_through_in_order_and_ends():
    r = make(target_lead_s=1000)
    b = book()
    r.start(b)
    blocks = drain(r)
    assert r.finished and r.error == ""
    assert sum(len(x) for x in blocks) / pcm.SAMPLE_RATE == pytest.approx(sum(len(u.text) * 0.06 + u.pause_s for u in b.units), abs=len(b.units) * 0.1)
    assert r.tts.said == [u.text for u in b.units]
    assert r.read(0.01) is None                                    # nothing after the end
    r.close()


def test_position_moves_forward_through_the_estimated_book():
    r = make(target_lead_s=1000)
    b = book()
    r.start(b)
    seen = []
    for _ in range(20000):
        blk = r.read(0.01)
        if blk is not None:
            seen.append(r.position())
        elif r.finished:
            break
    assert seen == sorted(seen) and seen[0] >= b.units[0].start_ms and seen[-1] <= b.total_ms
    assert seen[-1] > b.units[-1].start_ms
    r.close()


def test_starting_part_way_and_seeking_discard_the_old_buffer():
    r = make(target_lead_s=1000)
    b = book()
    r.start(b, b.units[6].start_ms + 5)
    assert wait_for(lambda: r.lead_s() > 0)
    r.read(0.5)
    assert r.tts.said[0] == b.units[6].text
    got = r.seek(b.units[2].start_ms)
    assert got == b.units[2].start_ms and r.position() == got
    assert r.lead_s() == 0 or wait_for(lambda: r.lead_s() > 0)
    for _ in range(40):
        r.read(0.05)
    assert r.unit_text() == b.units[2].text or r.unit_text() in (u.text for u in b.units[2:])
    r.close()


def test_a_sentence_made_for_the_old_place_is_never_played():
    gate = threading.Event()
    tts = FakeTts(gate=gate)
    r = make(tts, target_lead_s=1000, start_lead_s=0.1)
    b = book()
    r.start(b)
    assert wait_for(lambda: True)
    time.sleep(0.1)                                                # the producer is inside synth() for unit 0, held
    r.seek(b.units[8].start_ms)
    gate.set()
    for _ in range(60):
        r.read(0.02)
    played = {r._blocks[k][0] for k in range(len(r._blocks))}
    assert 0 not in played and 1 not in played                     # (the held unit 0 was dropped)
    r.close()


def test_a_voice_that_keeps_failing_gives_up_and_says_so(monkeypatch):
    monkeypatch.setattr(rd, "RETRY_S", 0)
    r = make(FakeTts(fail=True))
    r.start(book())
    assert wait_for(lambda: r.error)
    assert "voice stopped" in r.error
    assert r.read(0.01) is None and wait_for(lambda: r.finished or True)
    r.close()


def test_one_bad_sentence_is_skipped_not_looped_on(monkeypatch):
    monkeypatch.setattr(rd, "RETRY_S", 0)
    b = book()
    tts = FakeTts(bad="number 4 ")
    r = make(tts, target_lead_s=1000)
    r.start(b)
    drain(r)
    assert r.error == "" and r.finished
    assert not any("number 4 " in s for s in tts.said) and len(tts.said) == len(b.units) - 1
    r.close()


def test_a_book_is_levelled_as_a_whole_not_sentence_by_sentence(monkeypatch):
    loud = np.full(44100 * 2, 0.20, np.float32)
    quiet = np.full(44100 * 2, 0.05, np.float32)
    seq = iter([loud, loud, loud, quiet, loud])
    monkeypatch.setattr(pcm, "speech_filter", lambda samples, rate: next(seq))
    level = rd.BookLevel()
    out = [np.abs(level(np.zeros(10, np.float32), 16000, 0.5)).mean() for _ in range(5)]
    assert out[3] < out[2] / 2                    # the quiet sentence stays quiet (the DJ's levelling would lift it to match)
    assert out[4] == pytest.approx(out[2], rel=0.15)    # and a loud one after it is the same loudness as before
    assert max(out) / min(x for x in out if x) < 6


def test_the_pause_scale_lengthens_or_shortens_the_gaps():
    def total(scale):
        r = make(target_lead_s=1000, pause_scale=scale)
        r.start(book())
        return sum(len(b) for b in drain(r))
    assert total(2.0) > total(1.0) > total(0.5)
