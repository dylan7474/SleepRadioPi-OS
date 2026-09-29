import time
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import noise, pcm
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config.settings import load
from sleepradiopi.io import presets as presets_mod


@pytest.mark.parametrize("kind,low_minus_mid", [("white", -9), ("pink", 0), ("brown", 9), ("blue", -18),
                                                 ("violet", -27)])
def test_each_colour_has_its_slope_and_the_music_s_loudness(kind, low_minus_mid) -> None:
    x = noise.NoiseGen(kind, seed=2).next(44100 * 6)
    rms = float(np.sqrt(np.mean(x ** 2))) / (pcm.LOUDNESS_TARGET_RMS * 32767)
    assert rms == pytest.approx(1.0, abs=0.05)
    X = np.abs(np.fft.rfft(x[:, 0])) ** 2
    f = np.fft.rfftfreq(len(x), 1 / 44100)
    band = lambda c: 10 * np.log10(X[(f >= c / 1.414) & (f < c * 1.414)].sum())
    assert band(125) - band(1000) == pytest.approx(low_minus_mid, abs=1.5)


def test_the_blocks_join_without_a_seam_and_the_sides_differ() -> None:
    g = noise.NoiseGen("pink", seed=3)
    g.next(noise.BLOCK // 2)                                         # (it fades in when switched on)
    x = np.concatenate([g.next(1000) for _ in range(200)])          # across several FFT blocks
    level = [float(np.sqrt(np.mean(c ** 2))) for c in np.array_split(x[:, 0], 40)]
    assert max(level) / min(level) < 1.5                              # no dips at the joins
    assert abs(np.corrcoef(x[:, 0], x[:, 1])[0, 1]) < 0.05            # a wide stereo hiss
    with pytest.raises(ValueError):
        noise.NoiseGen("tartan")


def _speaker(tmp_path: Path) -> tuple[SpeakerOutput, Path]:
    out = tmp_path / "speaker.raw"
    return SpeakerOutput(command=["sh", "-c", f"cat >> {out}"], volume=100), out


def _size(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


def test_the_noise_plays_on_while_the_programme_is_paused(tmp_path: Path) -> None:
    spk, out = _speaker(tmp_path)
    spk.set_enabled(False)                                            # paused, no show
    spk.set_noise(noise.NoiseGen("brown", seed=1))
    time.sleep(0.4)
    first = _size(out)
    time.sleep(0.4)
    assert first > 0 and _size(out) > first                           # the pump keeps it going
    spk.set_noise(None)
    time.sleep(0.3)
    assert spk._proc is None                                          # nothing to play: speaker off


def test_balance_and_the_sleep_fade(tmp_path: Path) -> None:
    spk, out = _speaker(tmp_path)
    spk.start()
    tone = np.full((1024, 2), 10000, np.int16)
    silent = type("Z", (), {"next": lambda self, n: np.zeros((n, 2), np.float32)})()

    def level(mix, fade_end=None, noise_gen=silent):
        spk.noise, spk.noise_mix, spk.fade_end = noise_gen, mix, fade_end
        before = _size(out)
        spk.write(tone)
        time.sleep(0.15)
        data = np.frombuffer(out.read_bytes()[before:], np.int16)
        return float(np.abs(data).mean()) if data.size else 0.0
    assert level(50) == pytest.approx(10000, rel=0.02)               # even: the programme at full
    assert level(75) == pytest.approx(5000, rel=0.02)                 # towards the noise: programme half
    assert level(100) == 0.0
    assert level(50, fade_end=time.monotonic() + 30) == pytest.approx(5000, rel=0.1)   # the fade: programme only
    white = noise.NoiseGen("white", seed=1)
    white.next(noise.BLOCK // 2)                                      # past its fade-in
    loud = level(100, fade_end=time.monotonic() + 1, noise_gen=white)
    assert loud > 1000                                                # the noise isn't faded
    spk.noise = None
    spk.stop()


def test_control_saves_it_and_a_button_toggles_it(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    spk, _ = _speaker(tmp_path)
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    ctl.set_noise(on=True, kind="deep", mix=70)
    st = ctl.status()["noise"]
    assert st["on"] and st["kind"] == "deep" and st["mix"] == 70 and st["kinds"]["deep"]["label"] == "Deep brown"
    s = load(conf)
    assert (s.noise_on, s.noise_kind, s.noise_mix) == (True, "deep", 70)
    with pytest.raises(ValueError):
        ctl.set_noise(kind="tartan")
    (tmp_path / "b").mkdir()
    again = SpeakerControl(_speaker(tmp_path / "b")[0], lambda: None, lambda: None,
                           noise=(s.noise_on, s.noise_kind, s.noise_mix))
    assert again.status()["noise"]["on"]                              # back on after a restart
    again.set_noise(on=False)

    class Station:
        source = None
        artist = profile = None
    p = presets_mod.Presets(Station(), ctl, None, [{"kind": "action", "action": "noise"}])
    p.press(0)
    assert not ctl.status()["noise"]["on"]
    p.press(0)
    assert ctl.status()["noise"]["on"]
    ctl.set_noise(on=False)
