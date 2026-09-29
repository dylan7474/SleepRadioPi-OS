import os
import time
from pathlib import Path

import numpy as np

from sleepradiopi.audio import speaker as speaker_mod
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput, gain
from sleepradiopi.io.knob import EV_KEY, EV_REL, EVENT, Knob, handle


def _event(etype: int, value: int, code: int = 0) -> bytes:
    return EVENT.pack(0, 0, etype, code, value)


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_gain_curve() -> None:
    assert gain(0) == 0.0
    assert gain(100) == 1.0
    assert abs(20 * np.log10(gain(98)) - -1.0) < 1e-9
    assert gain(150) == 1.0 and gain(-5) == 0.0


def _speaker(tmp_path: Path) -> tuple[SpeakerOutput, Path]:
    out = tmp_path / "played.raw"
    return SpeakerOutput(command=["sh", "-c", f"cat >> {out}"]), out


def test_speaker_plays_scaled_audio_and_stops_when_paused(tmp_path: Path) -> None:
    spk, out = _speaker(tmp_path)
    block = np.full((3000, 2), 10000, dtype=np.int16)
    spk.volume = 100
    spk.start()
    spk.write(block)
    spk.volume = 0
    spk.write(block)
    spk.set_enabled(False)
    spk.write(block)          # paused: dropped
    spk.stop()
    played = np.frombuffer(out.read_bytes(), dtype=np.int16).reshape(-1, 2)
    assert len(played) == 6000
    assert (played[:3000] == 10000).all() and (played[3000:] == 0).all()


def test_speaker_mono_mixes_left_and_right(tmp_path: Path) -> None:
    out = tmp_path / "played.raw"
    spk = SpeakerOutput(command=["sh", "-c", f"cat >> {out}"], mono=True)
    block = np.column_stack([np.full(2000, 8000), np.full(2000, -2000)]).astype(np.int16)
    spk.volume = 100
    spk.start()
    spk.write(block)
    spk.volume = 88                   # -6 dB: about half
    spk.write(block)
    spk.stop()
    played = np.frombuffer(out.read_bytes(), dtype=np.int16).reshape(-1, 2)
    assert (played[:2000] == 3000).all()
    assert (abs(played[2000:].astype(int) - 1503) <= 1).all()


def test_control_joins_once_and_remembers_volume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(speaker_mod, "SAVE_AFTER_S", 0.05)
    spk, _ = _speaker(tmp_path)
    calls = []
    state = tmp_path / "state" / "speaker.json"
    ctl = SpeakerControl(spk, lambda: calls.append("join"), lambda: calls.append("leave"),
                         state_file=state, default_volume=40)
    status = ctl.status()
    assert status.pop("noise")["on"] is False
    assert status == {"volume": 40, "playing": False, "mono": False,
                      "test": None, "sleep_min": None, "sleep_left_s": None}
    ctl.play(); ctl.play(); ctl.toggle(); ctl.toggle()
    assert calls == ["join", "leave", "join"]
    ctl.set_volume(250)
    assert ctl.volume == 100
    ctl.step(-7)
    assert _wait(lambda: state.exists() and '"volume": 93' in state.read_text())
    again = SpeakerControl(spk, lambda: None, lambda: None, state_file=state, default_volume=40)
    assert again.volume == 93


def test_save_now_writes_the_volume_at_once(tmp_path: Path) -> None:
    spk, _ = _speaker(tmp_path)
    state = tmp_path / "state" / "speaker.json"
    ctl = SpeakerControl(spk, lambda: None, lambda: None, state_file=state)
    ctl.set_volume(61)            # normally saved a few seconds later
    ctl.save_now()                # e.g. just before a shutdown
    assert '"volume": 61' in state.read_text()


def test_handle_events() -> None:
    turns, presses = [], []
    data = _event(EV_REL, 1) + _event(EV_REL, -1) + _event(EV_KEY, 1, 164) + _event(EV_KEY, 0, 164)
    handle(data + b"\x00" * 5, turns.append, lambda: presses.append(1))
    assert turns == [1, -1] and presses == [1]


def test_knob_reads_input_devices(tmp_path: Path) -> None:
    fifo = tmp_path / "event0"
    os.mkfifo(fifo)
    turns, presses = [], []
    Knob(turns.append, lambda: presses.append(1), devices=tmp_path).start()
    assert _wait(lambda: True)
    fd = os.open(fifo, os.O_WRONLY)   # blocks until the knob has opened it
    os.write(fd, _event(EV_REL, 1) + _event(EV_REL, 1) + _event(EV_KEY, 1))
    assert _wait(lambda: turns == [1, 1] and presses == [1])
    os.close(fd)


def test_sleep_timer_fades_the_speaker_then_pauses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(speaker_mod, "SLEEP_FADE_S", 0.4)
    out = tmp_path / "played.raw"
    spk = SpeakerOutput(command=["sh", "-c", f"cat >> {out}"])
    calls = []
    ctl = SpeakerControl(spk, lambda: calls.append("join"), lambda: calls.append("leave"))
    spk.volume = 100
    spk.start()                                # the show is running
    ctl.play()
    block = np.full((1024, 2), 10000, dtype=np.int16)
    ctl.set_sleep(0.6 / 60)                    # 0.6 s: full volume, then a 0.4 s fade
    assert ctl.status()["sleep_min"] == 0.01 and ctl.status()["sleep_left_s"] in (0, 1)
    spk.write(block)                           # before the fade: full volume
    time.sleep(0.4)
    spk.write(block)                           # halfway through the fade
    # paused is set just before leave() is called: wait for both
    assert _wait(lambda: ctl.paused and calls == ["join", "leave"], timeout=2)
    assert spk.fade_end is None
    assert ctl.status()["sleep_left_s"] is None
    played = np.frombuffer(out.read_bytes(), dtype=np.int16).reshape(-1, 2)
    assert (played[:1024] == 10000).all()
    assert 2000 < played[1024:, 0].mean() < 8000
    ctl.play()                                 # the next play is at full volume again
    assert spk.fade_end is None


def test_pausing_or_cancelling_ends_the_sleep_timer(tmp_path: Path) -> None:
    spk, _ = _speaker(tmp_path)
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    ctl.play()
    ctl.set_sleep(30)
    assert ctl.status()["sleep_min"] == 30 and 1790 < ctl.status()["sleep_left_s"] <= 1800
    ctl.set_sleep(0)
    assert spk.fade_end is None and ctl.status()["sleep_min"] is None
    ctl.set_sleep(15)
    ctl.pause()                                # e.g. the knob
    assert spk.fade_end is None and ctl._sleep_timer is None


def test_a_card_that_wont_open_is_retried_ever_more_slowly_then_reported(monkeypatch) -> None:
    import numpy as np
    from sleepradiopi.audio import speaker as sp
    out = sp.SpeakerOutput(command=["false"])            # "aplay" that exits at once
    monkeypatch.setattr(sp, "STUCK_S", 0.5)
    naps = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: naps.append(s))
    stuck = []
    out.on_stuck = lambda: stuck.append(1)
    monkeypatch.setattr(sp.threading, "Thread", lambda target, **k: type("T", (), {"start": lambda self: target()})())
    monkeypatch.setattr(out, "_opened_once", True)
    out.start()
    t0 = sp.time.monotonic()
    while sp.time.monotonic() - t0 < 1.0:
        out.write(np.zeros((1024, 2), np.int16))
    backoff = [n for n in naps if n >= 0.1]                 # (other threads nap too)
    assert backoff[:4] == [0.1, 0.2, 0.4, 0.8] and max(backoff) <= 5.0
    assert stuck == [1]                                     # once
    out.stop()
