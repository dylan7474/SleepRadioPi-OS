"""Asked to stop (SIGTERM), the radio mutes the amp first, then goes (main._quiet_on_stop): no click."""
import signal

from sleepradiopi import main as main_mod


def test_a_stop_mutes_the_amp_before_going(monkeypatch) -> None:
    said = []

    class Amp:
        def mute(self):
            said.append("muted")
    monkeypatch.setattr(main_mod.os, "_exit", lambda code: said.append(f"exit {code}"))
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)
    was = signal.getsignal(signal.SIGTERM)
    try:
        main_mod._quiet_on_stop(Amp())
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, was)
    assert said == ["muted", "exit 0"]


def test_with_no_amp_pin_it_just_goes(monkeypatch) -> None:
    said = []
    monkeypatch.setattr(main_mod.os, "_exit", lambda code: said.append(code))
    was = signal.getsignal(signal.SIGTERM)
    try:
        main_mod._quiet_on_stop(None)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, was)
    assert said == [0]
