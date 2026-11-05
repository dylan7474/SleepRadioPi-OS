"""EQ mode on the case: hold button 1 for 3 s, buttons 1-4 pick a sound, hold button 1 again to keep it."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import eq as eq_mod
from sleepradiopi.audio.eq import Equalizer, validate_slots
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config.settings import load
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.io.presets import Presets
from sleepradiopi.web.server import make_handler

from test_offline import _station

SHOW = {"kind": "show", "profile": None, "artist": None}


def _rig(tmp_path: Path, with_eq: bool = True, config: dict | None = None):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps(config or {}))
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(44_100, 2) if with_eq else None)
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    clips = []
    ctl.play_clip = lambda clip: clips.append(clip.label)
    st = _station(tmp_path)
    st.tts = None
    p = Presets(st, ctl, conf, [SHOW, SHOW, SHOW, SHOW])
    played = []
    p._step = lambda index, k, quiet=False: played.append(index)     # a press that would play
    return ctl, p, conf, clips, played


def _gains(ctl) -> dict:
    return {k: v for k, v in ctl.speaker.eq.gains.items() if v}


def _saved(conf: Path) -> dict:
    return json.loads(conf.read_text())


def test_hold_button_one_enters_buttons_pick_sounds_and_hold_again_saves(tmp_path: Path) -> None:
    ctl, p, conf, clips, played = _rig(tmp_path)
    before = conf.read_text()
    p.hold(0)
    assert p.eq_active and p.status()["eq_mode"] is True
    p.press(1)                                              # button 2: Warm (the default)
    assert _gains(ctl) == {k: v for k, v in eq_mod.preset_bands("Warm").items() if v}
    p.press(0)                                              # button 1: Speech
    assert _gains(ctl) == {k: v for k, v in eq_mod.preset_bands("Speech").items() if v}
    assert conf.read_text() == before                       # trying sounds writes nothing to the card
    assert played == []                                     # and plays nothing
    p.hold(0)                                               # held again: keep it, back to normal
    assert not p.eq_active
    assert _saved(conf)["speaker_eq"] == eq_mod.clamp(eq_mod.preset_bands("Speech"))
    p.press(2)                                              # normal again: a press plays the preset
    assert played == [2]


def test_other_buttons_held_are_still_just_a_press(tmp_path: Path) -> None:
    ctl, p, conf, clips, played = _rig(tmp_path)
    p.hold(2)
    assert not p.eq_active and played == [2]


def test_a_saved_sound_brings_its_low_cut_and_mono(tmp_path: Path) -> None:
    sounds = [{"name": "Bass port", "eq": {"g125": 5}, "highpass": 140, "mono": True}]
    ctl, p, conf, clips, played = _rig(tmp_path, config={"speaker_sounds": sounds, "eq_slots": ["Bass port", None, None, None]})
    ctl.set_eq_slots(["Bass port", None, None, None])
    p.hold(0)
    p.press(0)
    assert ctl.speaker.eq.gains["g125"] == 5 and ctl.speaker.eq.highpass == 140 and ctl.speaker.mono
    assert "speaker_highpass_hz" not in _saved(conf)        # not yet
    p.hold(0)
    assert _saved(conf)["speaker_highpass_hz"] == 140 and _saved(conf)["speaker_mono"] is True


def test_an_empty_button_beeps_and_changes_nothing(tmp_path: Path) -> None:
    ctl, p, conf, clips, played = _rig(tmp_path)
    ctl.set_eq_slots([None, "No such sound", "Rock", None])
    p.hold(0)
    n = len(clips)
    p.press(0)
    p.press(1)
    assert _gains(ctl) == {} and len(clips) == n + 2


def test_eq_mode_ends_by_itself_and_keeps_the_sound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(presets_mod, "EQ_IDLE_S", 0.2)
    ctl, p, conf, clips, played = _rig(tmp_path)
    p.hold(0)
    p.press(3)                                              # Late night
    time.sleep(0.5)
    assert not p.eq_active
    assert _saved(conf)["speaker_eq"] == eq_mod.clamp(eq_mod.preset_bands("Late night"))


def test_a_radio_with_no_eq_has_no_eq_mode(tmp_path: Path) -> None:
    ctl, p, conf, clips, played = _rig(tmp_path, with_eq=False)
    p.hold(0)
    assert not p.eq_active and played == [0]


def test_the_service_menu_chord_ends_eq_mode(tmp_path: Path) -> None:
    ctl, p, conf, clips, played = _rig(tmp_path)
    p.hold(0)
    p.eq_leave()
    assert not p.eq_active and p.status()["eq_mode"] is False


def test_slots_are_checked_and_set_from_the_api(tmp_path: Path) -> None:
    assert validate_slots(["Rock", None, " Late  night ", "x"]) == ["Rock", None, "Late night", "x"]
    for bad in (["a"], "Rock", ["a", "b", "c", 5], [1, 2, 3, 4], ["x" * 41, None, None, None]):
        with pytest.raises(ValueError):
            validate_slots(bad)
    ctl, p, conf, clips, played = _rig(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p.station, None, ctl, config_file=conf, presets=p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/speaker"

    def post(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    try:
        assert post({"eq_slots": ["Jazz", "Dance", None, "Pop"]})["eq_slots"] == ["Jazz", "Dance", None, "Pop"]
        assert load(conf).eq_slots == ["Jazz", "Dance", None, "Pop"]
        with pytest.raises(urllib.error.HTTPError) as err:
            post({"eq_slots": ["Jazz"]})
        assert err.value.code == 400
    finally:
        httpd.shutdown()


def test_the_pages_ready_made_sounds_match_the_radios() -> None:
    html = (Path(__file__).parent.parent / "sleepradiopi" / "web" / "desktop.html").read_text()
    block = html[html.index("const EQ_PRESETS = {"):html.index("const eqPresetBands")]
    rows = {n: [int(x) for x in v.split(",")] for n, v in re.findall(r'"([^"]+)":\s*\[([^\]]+)\]', block)}
    assert rows == eq_mod.PRESETS
    assert set(eq_mod.DEFAULT_SLOTS) <= set(eq_mod.PRESETS)
