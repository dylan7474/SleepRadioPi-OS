"""amp_keep_awake: an amp with no MUTE pin that sleeps when the I2S clock stops
(the MAX98357A) clicks as it wakes, so with the setting the sound card stays
open through a pause, playing silence, and the same aplay carries on after."""
import time

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.audio.speaker import SpeakerOutput


def _speaker(tmp_path, keep_awake):
    out = tmp_path / "card.raw"
    sp = SpeakerOutput(command=["sh", "-c", f"cat > {out}"], keep_awake=keep_awake)   # (stands in for aplay)
    sp._opened_once = True                       # (no start-up sound to wait for)
    return sp, out


def _play_then_pause(sp):
    sp.start()
    sp.write(np.full((4096, pcm.CHANNELS), 1000, np.int16))
    proc = sp._proc
    sp.set_enabled(False)                        # paused
    return proc


def test_kept_awake_the_card_stays_open_through_a_pause_playing_silence(tmp_path):
    sp, out = _speaker(tmp_path, keep_awake=True)
    try:
        proc = _play_then_pause(sp)
        assert sp._proc is proc and proc.poll() is None          # the same aplay, still open
        time.sleep(0.15)
        size = out.stat().st_size
        time.sleep(0.4)
        grown = out.stat().st_size - size
        assert grown > 0                                          # silence keeps flowing (the amp's clock never stops)
        tail = np.frombuffer(out.read_bytes()[-grown // 4 * 4:], np.int16)
        assert not tail.any()                                     # and it is silence
        sp.set_enabled(True)                                      # play again: no reopen (no wake-up click)
        assert sp._proc is proc
    finally:
        sp.keep_awake = False
        sp.stop()
        with sp._lock:
            sp._close()


def test_without_it_a_pause_closes_the_card_as_before(tmp_path):
    sp, _ = _speaker(tmp_path, keep_awake=False)
    proc = _play_then_pause(sp)
    assert sp._proc is None
    proc.wait(timeout=2)
    sp.stop()


def test_it_is_this_radios_own_setting_never_saved_to_a_file():
    from sleepradiopi.config.backup import LOCAL
    from sleepradiopi.config.settings import Settings
    assert Settings().amp_keep_awake is False and "amp_keep_awake" in LOCAL


def test_kept_awake_with_a_mute_pin_the_amp_is_off_while_paused_and_the_card_never_stops(tmp_path):
    """Both settings (Phonosphere's MAX98357A): starting the card clicks even with the amp shut down,
    switching its SD pin with the clock running doesn't. So the card stays open, the pin goes low on
    pause and high on play."""
    from sleepradiopi.audio.ampmute import AmpMute
    pin = tmp_path / "value"
    pin.write_text("0")
    sp, _ = _speaker(tmp_path, keep_awake=True)
    sp.amp = AmpMute(pin, delay_s=0.02, status=None)
    try:
        sp.start()
        sp.write(np.full((4096, pcm.CHANNELS), 1000, np.int16))
        time.sleep(0.1)
        assert pin.read_text() == "1"
        proc = sp._proc
        sp.set_enabled(False)                     # pause: amp off, card on
        assert pin.read_text() == "0" and sp._proc is proc and proc.poll() is None
        sp.set_enabled(True)                      # play: amp on at once, same card
        assert pin.read_text() == "1" and sp._proc is proc
    finally:
        sp.keep_awake = False
        sp.stop()
        with sp._lock:
            sp._close()


def test_kept_awake_a_show_that_stalls_is_filled_with_silence(tmp_path):
    """A station changing or reconnecting writes nothing for seconds while it still 'plays': the
    card would run dry (an underrun restarts it: a click on a MAX98357A). Kept awake, silence fills it."""
    sp, out = _speaker(tmp_path, keep_awake=True)
    try:
        sp.start()
        sp.write(np.full((4096, pcm.CHANNELS), 1000, np.int16))
        proc = sp._proc
        time.sleep(0.4)                              # the show goes quiet but is still on
        size = out.stat().st_size
        time.sleep(0.4)
        assert out.stat().st_size > size             # silence went on flowing
        assert sp._proc is proc
    finally:
        sp.keep_awake = False
        sp.stop()
        with sp._lock:
            sp._close()


def test_not_kept_awake_a_stalled_show_is_left_alone(tmp_path):
    sp, out = _speaker(tmp_path, keep_awake=False)
    try:
        sp.start()
        sp.write(np.full((4096, pcm.CHANNELS), 1000, np.int16))
        time.sleep(0.4)
        size = out.stat().st_size
        time.sleep(0.4)
        assert out.stat().st_size == size            # as before: nothing extra written
    finally:
        sp.stop()


def test_kept_awake_a_bursty_stream_gets_no_silence_between_its_bursts(tmp_path):
    """An internet stream arrives in bursts; the card's buffer covers the gaps. Silence put in
    whenever the show hadn't written for a moment chopped the music into crackle (2026-11-14):
    it may only go in when the card is nearly dry. And waiting must not spin the CPU."""
    import os
    sp, out = _speaker(tmp_path, keep_awake=True)
    try:
        sp.start()
        burst = np.full((int(0.8 * pcm.SAMPLE_RATE), pcm.CHANNELS), 1000, np.int16)   # 0.8 s of sound
        cpu0 = sum(os.times()[:2])
        for _ in range(3):
            sp.write(burst)                      # (cat takes it at once: the card is 0.8 s ahead)
            time.sleep(0.4)                      # longer than SHOW_STALL_S, but the buffer still holds 0.4 s
        cpu = sum(os.times()[:2]) - cpu0
        sp.keep_awake = False                    # (stop the pump before reading)
        sp.stop()
        with sp._lock:
            sp._close()
        samples = np.frombuffer(out.read_bytes(), np.int16).reshape(-1, pcm.CHANNELS)
        sound = np.flatnonzero(samples[:, 0])
        music = samples[sound[0]:sound[-1] + 1, 0]       # (silence before the first burst is right: nothing yet)
        assert len(music) == 3 * len(burst) and music.all()   # the bursts back to back: no silence slipped between
        assert cpu < 0.6                          # (it waited, not spun: about 1.2 s of wall time)
    finally:
        sp.keep_awake = False
        sp.stop()
