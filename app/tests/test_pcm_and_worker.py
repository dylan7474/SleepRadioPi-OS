import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.tts.worker import clauses


def test_edge_trim_matches_trackprobe_rules():
    assert pcm.edge_trim(200_000, 1_000, 190_000) == (950, 190_150)
    assert pcm.edge_trim(200_000, 100, 199_900) == (0, 0)       # both edges too short to trim
    assert pcm.edge_trim(200_000, 6_000, 150_000) == (0, 0)     # lead too long, tail too long
    assert pcm.edge_trim(5_000, 4_000, 3_000) == (0, 0)         # start after end collapses


def test_sound_bounds():
    quiet, loud = 1e-8, 0.01
    env = np.array([quiet, quiet, loud, loud, quiet])
    assert pcm.sound_bounds(env) == (40, 80)
    assert pcm.sound_bounds(np.array([quiet, quiet])) == (-1, -1)


def test_gain_is_clamped():
    assert pcm.rms_to_gain(0.11) == 1.0
    assert pcm.rms_to_gain(0.001) == pcm.MAX_ITEM_GAIN
    assert pcm.rms_to_gain(1.0) == pcm.MIN_ITEM_GAIN
    assert pcm.rms_to_gain(0.0) == 1.0


def test_soft_limit_compresses_instead_of_clipping():
    out = pcm.apply_gain(np.array([30_000, -30_000, 1_000], dtype=np.int16), 2.0)
    assert out[2] == 2_000
    assert 32_000 < out[0] <= 32_767 and -32_768 <= out[1] < -32_000


def test_clauses_split_with_pauses():
    parts = clauses("That was Pink Moon, by Nick Drake. Coming up, Money.")
    assert [p for p, _ in parts] == ["That was Pink Moon, by Nick Drake.", "Coming up, Money."]
    assert [pause for _, pause in parts] == [0.30, 0.0]
    ip = "My address is one nine two, dot, one six eight, dot, five zero, dot, one three zero."
    assert [len(p.split()) for p, _ in clauses(ip)] == [11, 6]       # not a dozen tiny pieces


def test_long_clauses_are_split_at_spaces() -> None:
    from sleepradiopi.tts.worker import MAX_WORDS, clauses
    words = " ".join(f"w{i}" for i in range(30))
    parts = clauses(words + ". Short one.")
    assert [len(c.split()) for c, _ in parts] == [MAX_WORDS, MAX_WORDS, 30 - 2 * MAX_WORDS, 2]


def test_a_worker_past_its_limit_hands_the_rest_to_a_fresh_one(monkeypatch) -> None:
    import threading as th
    import multiprocessing as mp
    from types import SimpleNamespace
    from sleepradiopi.tts import engine as engine_mod, worker as worker_mod

    class Engine:
        def __init__(self, num_threads): ...
        def ensure_loaded(self, d, v): return True
        def synth(self, text, speed):
            return SimpleNamespace(samples=np.full(100, len(text), dtype=np.float32), sample_rate=1000)
    monkeypatch.setattr(engine_mod, "OfflineTtsEngine", Engine)
    monkeypatch.setattr(worker_mod.os, "nice", lambda n: None)
    rss = iter([150, 150, 250, 150, 150, 150, 150, 150])       # past the limit after the 2nd clause
    monkeypatch.setattr(worker_mod, "_rss_mb", lambda: next(rss, 150))
    parent, child = mp.Pipe()
    t = th.Thread(target=worker_mod._serve, args=(child, "x", "v"), daemon=True)
    t.start()
    assert parent.recv()[0] == "ready"
    parent.send(("One two. Three four. Five six. Seven eight.", 1.0, 240))
    status, payload, rate, rss_mb, rest = parent.recv()
    assert status == "ok" and rest == "Five six. Seven eight."   # these go to a fresh worker
    parent.send(None)

    w = worker_mod.TtsWorker.__new__(worker_mod.TtsWorker)          # (no real process)
    w.voice, w.recycle_mb, w.hard_mb, w.last_rss_mb = "v", 200, 240, 0
    w._lock, w._ready = th.Lock(), th.Event()
    w._ready.set()
    sent = []
    replies = iter([("ok", np.ones(10, np.float32).tobytes(), 1000, 250, "the rest."),
                    ("ok", np.ones(5, np.float32).tobytes(), 1000, 160, "")])
    w._conn = SimpleNamespace(send=sent.append, recv=lambda: next(replies))
    w._recycle = lambda: w._ready.set()                            # (a fresh worker, at once)
    samples, rate = w.synth("v", "Some words, the rest.", 1.0)
    assert [m[0] for m in sent] == ["Some words, the rest.", "the rest."]
    assert len(samples) == 10 + int(1000 * worker_mod.SENTENCE_PAUSE_S) + 5
