"""The sound by what's playing: each kind of audio can have its own EQ; a sound picked by hand holds
until the kind of audio changes."""
import json
from pathlib import Path

import pytest

from sleepradiopi.audio import eq as eq_mod
from sleepradiopi.audio.eq import Equalizer, validate_by_type
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.backup import BadSettings, _check
from sleepradiopi.playback.ebooks import EBOOK


def _rig(tmp_path: Path, config: dict | None = None):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps(config or {}))
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(44_100, 2))
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    return ctl, conf


def _gains(ctl) -> dict:
    return {k: v for k, v in ctl.speaker.eq.gains.items() if v}


def _bands(name: str) -> dict:
    return {k: v for k, v in eq_mod.clamp(eq_mod.preset_bands(name)).items() if v}


def test_audio_type_of_a_source() -> None:
    t = Station.audio_type_of
    assert t(None) == "music"
    assert t({"kind": "album"}) == "music" and t({"kind": "playlist"}) == "music"
    assert t({"name": "x", "url": "http://x"}) == "radio" and t({"kind": "radio"}) == "radio"
    assert t({"kind": "episode"}) == "podcast"
    assert t({"kind": "book", "key": "Dickens/Oliver"}) == "audiobook"
    assert t({"kind": "book", "key": "kept:Talks/one"}) == "audiobook"
    assert t({"kind": "book", "key": EBOOK + "Austen.epub"}) == "ebook"


def test_a_kind_gets_its_sound_and_leaving_it_gives_yours_back_without_saving(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_eq(eq_mod.preset_bands("Warm"))                       # the listener's own (saved)
    saved = conf.read_text()
    ctl.set_audio_type("music")                                   # music: keep mine
    assert _gains(ctl) == _bands("Warm")
    ctl.set_audio_type("audiobook")                               # audiobook: Speech
    assert _gains(ctl) == _bands("Speech")
    ctl.set_audio_type("podcast")                                 # (also Speech)
    assert _gains(ctl) == _bands("Speech")
    ctl.set_audio_type("music")                                   # back to music: Warm again
    assert _gains(ctl) == _bands("Warm")
    assert conf.read_text() == saved                              # nothing written to the card
    assert ctl.status()["audio_type"] == "music" and ctl.status()["eq_from_type"] is False


def test_a_hand_picked_sound_holds_until_the_kind_changes(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path, {"eq_by_type": {"music": "Rock"}})
    ctl.eq_by_type = validate_by_type({"music": "Rock", "audiobook": "Speech"})
    ctl.set_audio_type("music")
    assert _gains(ctl) == _bands("Rock")
    ctl.set_eq(eq_mod.preset_bands("Late night"))                 # the listener picks Late night by hand
    ctl.set_audio_type("music")                                   # same kind again: it holds
    assert _gains(ctl) == _bands("Late night")
    ctl.set_audio_type("audiobook")                               # kind changes: Speech takes over
    assert _gains(ctl) == _bands("Speech")
    ctl.set_audio_type("music")                                   # and music's own Rock comes back
    assert _gains(ctl) == _bands("Rock")


def test_a_hand_pick_comes_back_for_a_kind_with_no_sound(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_audio_type("music")
    ctl.set_eq(eq_mod.preset_bands("Jazz"))
    ctl.set_audio_type("audiobook")
    assert _gains(ctl) == _bands("Speech")
    ctl.set_audio_type("music")                                   # music has none: the Jazz they picked
    assert _gains(ctl) == _bands("Jazz")


def test_a_hand_pick_during_a_kinds_sound_becomes_theirs(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_audio_type("music")
    ctl.set_audio_type("audiobook")                               # Speech, from the kind
    ctl.set_eq(eq_mod.preset_bands("Dance"), save=False)          # EQ mode picks Dance
    assert _gains(ctl) == _bands("Dance")
    ctl.set_audio_type("music")
    assert _gains(ctl) == _bands("Dance")                         # music keeps theirs, not the old one


def test_off_leaves_the_sound_alone_and_turning_it_off_gives_yours_back(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_eq(eq_mod.preset_bands("Warm"))
    ctl.set_audio_type("ebook")
    assert _gains(ctl) == _bands("Speech")
    ctl.set_eq_auto(False)                                        # off, while it's on: yours back
    assert _gains(ctl) == _bands("Warm")
    ctl.set_audio_type("audiobook")
    assert _gains(ctl) == _bands("Warm")
    assert json.loads(conf.read_text())["eq_auto"] is False
    ctl.set_eq_auto(True)                                         # on again: audiobooks get Speech at once
    assert _gains(ctl) == _bands("Speech")


def test_changing_the_mapping_applies_to_what_is_playing_and_is_saved(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_audio_type("music")
    ctl.set_eq_auto(True, {"music": "Rock"})
    assert _gains(ctl) == _bands("Rock")
    assert json.loads(conf.read_text())["eq_by_type"]["music"] == "Rock"
    ctl.set_eq_auto(True, {"music": None})
    assert _gains(ctl) == {}                                      # (their own was flat)


def test_a_saved_sound_with_mono_and_low_cut_is_used(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path, {"speaker_sounds": [{"name": "Kitchen", "eq": {"g1k": 3}, "highpass": 140, "mono": True}]})
    ctl.eq_by_type = validate_by_type({"audiobook": "Kitchen"})
    ctl.set_audio_type("audiobook")
    assert ctl.speaker.mono and ctl.speaker.eq.highpass == 140 and _gains(ctl)["g1k"] == 3
    ctl.set_audio_type("music")
    assert not ctl.speaker.mono and ctl.speaker.eq.highpass == 0


def test_an_unknown_sound_name_changes_nothing(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.eq_by_type = validate_by_type({"audiobook": "Nonesuch"})
    ctl.set_eq(eq_mod.preset_bands("Warm"))
    ctl.set_audio_type("audiobook")
    assert _gains(ctl) == _bands("Warm")


def test_validate_by_type() -> None:
    assert validate_by_type({"ebook": "Speech"})["music"] is None
    with pytest.raises(ValueError):
        validate_by_type({"karaoke": "Rock"})
    with pytest.raises(ValueError):
        validate_by_type({"music": 5})
    with pytest.raises(ValueError):
        validate_by_type([])
    with pytest.raises(BadSettings):
        _check("eq_by_type", {"karaoke": "Rock"})
    assert _check("eq_by_type", {"music": "Rock"})["music"] == "Rock"


# --- a sound on a button ---------------------------------------------------------------------

from sleepradiopi.io import presets as presets_mod


def test_a_preset_can_carry_a_sound_only_if_it_plays_something() -> None:
    p = presets_mod.validate({"kind": "radio", "name": "R4", "url": "http://x/y", "sound": "  Speech "})
    assert p["sound"] == "Speech"
    assert "sound" not in presets_mod.validate({"kind": "radio", "name": "R4", "url": "http://x/y"})
    assert "sound" not in presets_mod.validate({"kind": "radio", "name": "R4", "url": "http://x/y", "sound": ""})
    assert "sound" not in presets_mod.validate({"kind": "action", "action": "time", "sound": "Speech"})
    with pytest.raises(ValueError):
        presets_mod.validate({"kind": "radio", "name": "R4", "url": "http://x/y", "sound": 5})
    assert presets_mod.audio_type_of({"kind": "book", "key": EBOOK + "a"}) == "ebook"
    assert presets_mod.audio_type_of({"kind": "book", "key": "a"}) == "audiobook"
    assert presets_mod.audio_type_of({"kind": "show"}) == "music"


def test_a_buttons_sound_beats_the_kind_and_holds_over_the_station_starting(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_eq(eq_mod.preset_bands("Warm"))
    saved = conf.read_text()
    ctl.set_audio_type("radio")                                   # radio: keep mine
    ctl.set_button_sound("Rock", "radio")                         # a button: Rock on this radio station
    assert _gains(ctl) == _bands("Rock")
    ctl.set_audio_type("radio")                                   # (the station starting: same kind)
    assert _gains(ctl) == _bands("Rock")
    ctl.set_button_sound("Speech", "radio")                       # another radio button: its own sound
    assert _gains(ctl) == _bands("Speech")
    ctl.set_button_sound(None, "radio")                           # a radio button with none: back to the kind (mine)
    assert _gains(ctl) == _bands("Warm")
    assert conf.read_text() == saved


def test_a_button_with_no_sound_changes_nothing_if_none_was_on(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_audio_type("music")
    ctl.set_eq(eq_mod.preset_bands("Jazz"))                       # a hand pick
    ctl.set_button_sound(None, "music")
    assert _gains(ctl) == _bands("Jazz")


def test_a_hand_pick_beats_a_buttons_sound_until_the_next_press(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_button_sound("Rock", "music")
    ctl.set_eq(eq_mod.preset_bands("Late night"), save=False)
    ctl.set_audio_type("music")
    assert _gains(ctl) == _bands("Late night")
    ctl.set_button_sound(None, "music")                           # (the hand pick is now theirs: stays)
    assert _gains(ctl) == _bands("Late night")
    ctl.set_button_sound("Rock", "music")
    assert _gains(ctl) == _bands("Rock")


def test_a_buttons_sound_ends_when_the_kind_changes_and_the_kind_rule_returns(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_eq(eq_mod.preset_bands("Warm"))
    ctl.set_button_sound("Rock", "music")
    ctl.set_audio_type("audiobook")                               # a book from the page: Speech by kind
    assert _gains(ctl) == _bands("Speech")
    ctl.set_audio_type("music")                                   # music from the page: none -> mine
    assert _gains(ctl) == _bands("Warm")


def test_button_sounds_are_off_with_automatic_off(tmp_path: Path) -> None:
    ctl, conf = _rig(tmp_path)
    ctl.set_eq(eq_mod.preset_bands("Warm"))
    ctl.set_eq_auto(False)
    ctl.set_button_sound("Rock", "music")
    assert _gains(ctl) == _bands("Warm")
