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


def test_a_named_radio_has_its_name_on_the_network(tmp_path: Path) -> None:
    from sleepradiopi.config import brand
    # every new radio is sleepradiopi, whatever its case; a name of your own changes it
    assert brand.hostname_for({}) == "sleepradiopi"
    assert brand.hostname_for({"hardware": "cathedral"}) == "sleepradiopi"
    assert brand.hostname_for({"station_name": "   "}) == "sleepradiopi"
    assert brand.hostname_for({"station_name": "Carisbrooke"}) == "carisbrooke"
    assert brand.hostname_for({"station_name": "  Dad's   Radio "}) == "dads-radio"
    assert brand.hostname_for({"station_name": "Café Nº 5!"}) == "cafe-no-5"
    assert brand.hostname_for({"station_name": "--Front_Room--"}) == "front-room"
    assert brand.hostname_for({"station_name": "!!!"}) == "sleepradiopi"
    assert brand.hostname_for({"station_name": "夜"}) == "sleepradiopi"
    long = brand.hostname_for({"station_name": "a" * 30 + " " + "b" * 40})
    assert len(long) <= 63 and not long.endswith("-")

    # on a PC (nowhere to ask) nothing happens
    file, request = tmp_path / "data" / "hostname", tmp_path / "run" / "hostname"
    file.parent.mkdir()
    assert brand.apply_hostname("carisbrooke", file, request, current="sleepradiopi") is False
    assert not file.exists()
    # on the radio: the name is kept for start-up, and asked for when the system has another
    request.parent.mkdir()
    assert brand.apply_hostname("carisbrooke", file, request, current="sleepradiopi") is True
    assert file.read_text() == "carisbrooke\n" and request.exists()
    request.unlink()
    assert brand.apply_hostname("carisbrooke", file, request, current="carisbrooke") is False
    assert not request.exists() and [p.name for p in file.parent.iterdir()] == ["hostname"]
    # un-named again: back to sleepradiopi
    assert brand.apply_hostname("sleepradiopi", file, request, current="carisbrooke") is True
    assert file.read_text() == "sleepradiopi\n"
    old = brand.name
    try:
        brand.set_name("Phonosphere")
        from sleepradiopi.broadcast import profiles
        from sleepradiopi.broadcast.script_builder import DjScriptBuilder, artist_station_name
        assert profiles.station_name("Dad") == "Dad Radio"           # a theme is a station of its own
        assert artist_station_name(None) == "Phonosphere" and DjScriptBuilder().station == "Phonosphere"
        from sleepradiopi.io.announce import announcement
        assert announcement([], "x") == "I'm not connected to a network."  # no name: it may play any station
        hotspot = announcement([], "x", {"ssid": "Phonosphere-Setup", "password": "ab", "ip": "192.168.4.1"})
        assert hotspot.startswith("I couldn't find") and "join Phonosphere-Setup" in hotspot   # (the network keeps it)
    finally:
        brand.set_name(old)


def test_save_setting_never_wipes_the_file(tmp_path) -> None:
    """2026-09-30: two saves at once shared one temp file, the config couldn't be
    read, and a save wrote {} plus its one key -- every other setting was lost."""
    import json, threading
    from sleepradiopi.config.settings import save_setting
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({f"k{i}": i for i in range(50)}))
    def many(n):
        for j in range(40):
            save_setting(cfg, f"t{n}", j)
    ts = [threading.Thread(target=many, args=(n,)) for n in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    conf = json.loads(cfg.read_text())
    assert all(conf[f"k{i}"] == i for i in range(50)) and all(conf[f"t{n}"] == 39 for n in range(6))
    assert not list(tmp_path.glob("*.tmp"))
    cfg.write_text('{"broken": ')                  # unreadable: nothing written, a copy kept
    save_setting(cfg, "x", 1)
    assert cfg.read_text() == '{"broken": ' and list(tmp_path.glob("config.json.unreadable-*"))
