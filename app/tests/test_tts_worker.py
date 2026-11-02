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


def test_switching_voice_loads_one_worker_and_a_wake_up_cannot_load_a_second(make_worker, tmp_path, monkeypatch) -> None:
    log = tmp_path / "loads.txt"
    real = worker_mod._serve

    def serve(conn, voice_dir, voice):             # (a child process: it records its loads in a file)
        with open(log, "a") as f:
            f.write(voice + "\n")
        real(conn, voice_dir, voice)
    monkeypatch.setattr(worker_mod, "_serve", serve)
    loads = lambda: log.read_text().split()        # noqa: E731
    w = make_worker()
    assert w.synth("v", "hello", 1.0)[1] == 22050 and loads() == ["v"]
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "model.onnx").write_bytes(b"x")
    (tmp_path / "v" / "model.onnx").write_bytes(b"x")
    assert w.use_voice("v") is True and w.use_voice("missing") is False
    assert w.use_voice("other") is True
    for _ in range(3):                             # lines asked for at once while it switches: still ONE load
        w.synth("other", "hello", 1.0)
    assert loads() == ["v", "other"] and w._loaded == "other"
    assert w.use_voice("v") is True
    w.synth("v", "again", 1.0)
    assert loads() == ["v", "other", "v"]
