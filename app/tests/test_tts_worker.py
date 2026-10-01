"""The voice worker comes back after the kernel kills it (out of memory)."""
from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.tts import worker as worker_mod


def _fake_serve(conn, voice_dir: str, voice: str) -> None:
    conn.send(("ready", 0.0, 50))
    while True:
        msg = conn.recv()
        if msg is None:
            return
        text = msg[0]
        if text == "die" or (text == "die-once" and not os.path.exists(voice_dir + "/died")):
            open(voice_dir + "/died", "w").close()
            os._exit(9)                          # (as the OOM killer does: no goodbye)
        conn.send(("ok", np.zeros(10, dtype=np.float32).tobytes(), 22050, 50, ""))


@pytest.fixture
def make_worker(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(worker_mod, "_serve", _fake_serve)
    fork = mp.get_context("fork")
    monkeypatch.setattr(worker_mod.mp, "get_context", lambda _m: fork)
    made = []

    def make():
        (tmp_path / "v").mkdir(exist_ok=True)
        w = worker_mod.TtsWorker(tmp_path, "v")
        made.append(w)
        return w
    yield make
    for w in made:
        w.close()


def test_a_killed_worker_is_replaced_and_the_line_still_said(make_worker) -> None:
    w = make_worker()
    samples, rate = w.synth("v", "hello", 1.0)
    assert len(samples) == 10
    samples, _ = w.synth("v", "die-once", 1.0)   # dies, a fresh one says it
    assert len(samples) == 10
    samples, _ = w.synth("v", "and after", 1.0)
    assert len(samples) == 10


def test_a_line_that_kills_every_worker_fails_but_the_next_one_works(make_worker) -> None:
    w = make_worker()
    with pytest.raises(RuntimeError):
        w.synth("v", "die", 1.0)
    samples, _ = w.synth("v", "fine", 1.0)
    assert len(samples) == 10
