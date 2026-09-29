from pathlib import Path

from sleepradiopi.config.settings import Settings, load, save


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    settings = load(tmp_path / "nope.json")
    assert settings == Settings()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = Settings(music_folder="/mnt/music", broadcast_chattiness="maximum")
    save(path, original)
    assert load(path) == original


def test_save_replaces_atomically_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.json"
    save(path, Settings(broadcast_voice="stock"))
    save(path, Settings(broadcast_voice="personal"))
    assert load(path).broadcast_voice == "personal"
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.json"]


def test_the_radios_name() -> None:
    from sleepradiopi.config import brand
    assert brand.name_for({}) == "Sleep Radio"
    assert brand.name_for({"hardware": "cathedral"}) == "Phonosphere"
    assert brand.name_for({"hardware": "cathedral", "station_name": "  Dad's   Radio "}) == "Dad's Radio"
    assert brand.hotspot_ssid("Phonosphere") == "Phonosphere-Setup"
    assert brand.hotspot_ssid("Sleep Radio") == "SleepRadio-Setup"
    old = brand.name
    try:
        brand.set_name("Phonosphere")
        from sleepradiopi.broadcast import profiles
        from sleepradiopi.broadcast.script_builder import DjScriptBuilder, artist_station_name
        assert profiles.station_name("Dad") == "Dad on Phonosphere"
        assert artist_station_name(None) == "Phonosphere" and DjScriptBuilder().station == "Phonosphere"
        from sleepradiopi.io.announce import announcement
        assert announcement([], "x").startswith("Phonosphere here.")
    finally:
        brand.set_name(old)
