import time

from sleepradiopi.audio.ampmute import AmpMute
from sleepradiopi.audio.speaker import SpeakerOutput


def test_mute_then_unmute_after_the_delay(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.05)
    amp.mute()
    assert pin.read_text() == "0"
    amp.unmute_soon()
    assert pin.read_text() == "0"            # not yet: the card's pop goes unheard
    time.sleep(0.2)
    assert pin.read_text() == "1"


def test_closed_again_before_the_delay_stays_muted(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.1)
    amp.unmute_soon()
    amp.mute()
    time.sleep(0.25)
    assert pin.read_text() == "0"


def test_a_missing_pin_does_nothing(tmp_path):
    amp = AmpMute(tmp_path / "nope", delay_s=0.01)
    amp.mute()
    amp.unmute_soon()
    time.sleep(0.05)
    assert not amp.available


def test_the_speaker_mutes_when_the_card_closes_and_unmutes_after_it_opens(tmp_path):
    pin = tmp_path / "value"
    pin.write_text("1")
    amp = AmpMute(pin, delay_s=0.05)
    sp = SpeakerOutput(command=["cat"], amp=amp)    # `cat` stands in for aplay
    sp.noise = object()                             # (anything that wants the card open)
    with sp._lock:
        sp._open()
    time.sleep(0.2)
    assert pin.read_text() == "1"
    with sp._lock:
        sp._close()
    assert pin.read_text() == "0"
