import time

from sleepradiopi.audio.ampmute import AmpMute
from sleepradiopi.audio.speaker import SpeakerOutput


def test_mute_then_unmute_after_the_delay(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.05, status=None)
    amp.mute()
    assert pin.read_text() == "0"
    amp.unmute_soon()
    assert pin.read_text() == "0"            # not yet: the card's pop goes unheard
    time.sleep(0.2)
    assert pin.read_text() == "1"


def test_closed_again_before_the_delay_stays_muted(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.1, status=None)
    amp.unmute_soon()
    amp.mute()
    time.sleep(0.25)
    assert pin.read_text() == "0"


def test_a_missing_pin_does_nothing(tmp_path):
    amp = AmpMute(tmp_path / "nope", delay_s=0.01, status=None)
    amp.mute()
    amp.unmute_soon()
    time.sleep(0.05)
    assert not amp.available


def test_the_speaker_mutes_when_the_card_closes_and_unmutes_after_it_opens(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.05, status=None)
    sp = SpeakerOutput(command=["cat"], amp=amp)    # `cat` stands in for aplay
    sp.noise = object()                             # (anything that wants the card open)
    with sp._lock:
        sp._open()
    time.sleep(0.2)
    assert pin.read_text() == "1"
    with sp._lock:
        sp._close()
    assert pin.read_text() == "0"


def test_it_waits_for_the_card_to_be_running_before_the_amp_goes_on(tmp_path):
    """A busy Pi at boot can take longer than the delay to start the card, and switching the amp
    on before its I2S clock runs is what clicks (a MAX98357A, tested 2026-11-14): so the unmute
    waits for a playback stream to say RUNNING, then the delay."""
    pin = tmp_path / "value"
    pin.write_text("0")
    status = tmp_path / "card0" / "pcm0p" / "sub0" / "status"
    status.parent.mkdir(parents=True)
    status.write_text("closed\n")
    amp = AmpMute(pin, delay_s=0.05, status=str(tmp_path / "card*" / "pcm*p" / "sub*" / "status"))
    amp.unmute_soon()
    time.sleep(0.3)
    assert pin.read_text() == "0"                  # the card isn't running yet: amp still off
    status.write_text("state: RUNNING\nowner_pid   : 1\n")
    time.sleep(0.03)
    assert pin.read_text() == "0"                  # running, but the moment after it hasn't passed
    time.sleep(0.15)
    assert pin.read_text() == "1"


def test_a_pause_while_waiting_for_the_card_keeps_it_off(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("0")
    status = tmp_path / "status"
    status.write_text("closed\n")
    amp = AmpMute(pin, delay_s=0.02, status=str(status))
    amp.unmute_soon()
    amp.mute()
    status.write_text("state: RUNNING\n")
    time.sleep(0.2)
    assert pin.read_text() == "0"
