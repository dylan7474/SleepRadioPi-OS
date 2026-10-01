"""Each station has its own jingles (Jingles/<theme>, Jingles/<the radio's name> for the
main show), a Power-on folder plays at start-up, and the folders are made by themselves."""

from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path

import pytest

from sleepradiopi.broadcast import station as station_mod
from sleepradiopi.config.settings import Settings
from sleepradiopi.media import MediaLibrary

from test_offline import FakeTts, NullOutput


def _station(tmp_path: Path, jingles: dict[str, list[str]]) -> station_mod.Station:
    for artist in ("ABBA", "Bread"):
        d = tmp_path / "music" / artist / "Hits"
        d.mkdir(parents=True)
        for n in (1, 2):
            (d / f"0{n} - {artist} {n}.mp3").write_bytes(b"x")
    for folder, names in jingles.items():
        d = tmp_path / "jingles" / folder if folder else tmp_path / "jingles"
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            (d / name).write_bytes(b"x")
    cfg = asdict(Settings())
    cfg.update(music_folder=tmp_path / "music", jingles_folder=tmp_path / "jingles", hooks_file="",
               scan_cache=tmp_path / "scans.json", tag_cache=None,
               profiles=[{"name": "Carisbrooke", "artists": ["Bread"]}])
    return station_mod.Station(cfg, FakeTts(), NullOutput())


def test_each_station_plays_only_its_own_jingles(tmp_path: Path) -> None:
    st = _station(tmp_path, {"Sleep Radio": ["Main.mp3"], "Carisbrooke": ["Cari 1.mp3", "Cari 2.mp3"],
                             "Power-on": ["Start.mp3"]})
    assert [j.path.name for j in st.jingles] == ["Main.mp3"]                  # the main show's
    st.set_profile("Carisbrooke")
    assert sorted(j.path.name for j in st.jingles) == ["Cari 1.mp3", "Cari 2.mp3"]
    assert st.builder.station == "Carisbrooke Radio"
    st.set_profiles([{"name": "Carisbrooke", "artists": ["Bread"]}, {"name": "Classical", "artists": ["ABBA"]}])
    st.set_profile("Classical")
    assert st.jingles == []                                                    # no folder / empty: none
    st.set_profile(None)
    assert [j.path.name for j in st.jingles] == ["Main.mp3"]


def test_power_on_plays_the_start_up_jingle_whatever_comes_next(tmp_path: Path) -> None:
    st = _station(tmp_path, {"Sleep Radio": ["Main.mp3"], "Power-on": ["Start.mp3"]})
    pending = Future()
    welcome = station_mod.Step("say", station_mod.Speech("Good evening", "stock", pending))
    steps = st._opening_steps([welcome], "power-on")
    assert [(s.kind, s.jingle.path.name) for s in steps] == [("jingle", "Start.mp3")] and pending.cancelled()
    done = Future()
    done.set_result(None)
    ready = station_mod.Step("say", station_mod.Speech("Good evening", "stock", done))
    steps = st._opening_steps([ready], "power-on")
    assert steps[0].jingle.path.name == "Start.mp3" and steps[1] is ready   # then the welcome
    back = st._opening_steps([station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))], "back to the show")
    assert all(s.jingle is None or s.jingle.path.name != "Start.mp3" for s in back)   # only at power-on


def test_the_folders_are_made_and_loose_jingles_move_to_the_main_show(tmp_path: Path) -> None:
    from sleepradiopi.main import jingle_folders
    st = _station(tmp_path, {"": ["Old 1.mp3", "Old 2.mp3"]})
    rescans = []
    media = MediaLibrary({"jingles": tmp_path / "jingles"}, on_changed=lambda kinds: rescans.append(kinds),
                         lease=tmp_path / "run" / "media-rw")
    jingle_folders(media, st)
    root = tmp_path / "jingles"
    assert {p.name for p in root.iterdir()} == {"Sleep Radio", "Power-on", "Carisbrooke"}
    assert sorted(p.name for p in (root / "Sleep Radio").iterdir()) == ["Old 1.mp3", "Old 2.mp3"]
    assert rescans == [{"jingles"}] and not (tmp_path / "run" / "media-rw").exists()
    st.on_profiles = lambda: jingle_folders(media, st)                  # (main wires it so)
    st.set_profiles([{"name": "Carisbrooke", "artists": ["Bread"]}, {"name": "Classical", "artists": ["ABBA"]}])
    assert (root / "Classical").is_dir()


def test_a_theme_name_must_make_a_folder() -> None:
    from sleepradiopi.broadcast import profiles
    for bad in ("AC/DC", "back\\slash", ".hidden"):
        with pytest.raises(ValueError):
            profiles.validate([{"name": bad, "artists": ["x"]}])


def test_a_theme_sets_its_own_dj_settings_and_the_rest_are_the_radios(tmp_path: Path) -> None:
    """Cascading: the radio's DJ settings, with the playing theme's own on top."""
    from sleepradiopi.broadcast.models import LinkKind
    st = _station(tmp_path, {"Sleep Radio": ["Main.mp3"]})
    st.set_dj(chattiness="maximum", news_enabled=True, time_checks=True, jingle_every=4)
    st.set_profiles([{"name": "Carisbrooke", "artists": ["Bread"],
                      "settings": {"news": False, "chattiness": "minimal", "time_checks": False}}])
    st.set_profile("Carisbrooke")
    assert (st.chattiness, st.config.news_enabled, st.time_checks, st.config.jingle_every) == ("minimal", False, False, 4)
    assert st.dj_settings()["chattiness"] == "maximum" and st.dj_settings()["news_enabled"]   # the DJ window: the radio's
    st.set_dj(chattiness="balanced", jingle_every=2)        # the radio's: the theme's own still wins
    assert st.chattiness == "minimal" and st.config.jingle_every == 2
    st.set_profile(None)                                    # the main show: all the radio's
    assert (st.chattiness, st.config.news_enabled, st.time_checks) == ("balanced", True, True)
    st.set_profile("Carisbrooke")
    st.set_profiles([{"name": "Carisbrooke", "artists": ["Bread"]}])   # its own taken off, while it plays
    assert (st.chattiness, st.config.news_enabled) == ("balanced", True)


def test_time_checks_off_make_them_links(tmp_path: Path, monkeypatch) -> None:
    from sleepradiopi.broadcast.models import LinkKind
    st = _station(tmp_path, {})
    st.set_dj(time_checks=False)
    monkeypatch.setattr(st._show_clock, "on_track_started", lambda now: LinkKind.TIME_CHECK)
    track = st._take_next()
    st._plan_gap(track, st._take_next())
    assert st._gap_decision[0] == LinkKind.LINK


def test_a_themes_settings_are_checked() -> None:
    from sleepradiopi.broadcast import profiles
    ok = profiles.validate([{"name": "A", "artists": ["x"], "settings": {"news": False, "jingle_every": 0, "chattiness": None}}])
    assert ok[0]["settings"] == {"news": False, "jingle_every": 0}
    assert "settings" not in profiles.validate([{"name": "A", "artists": ["x"], "settings": {}}])[0]
    for bad in ({"volume": 3}, {"news": "yes"}, {"chattiness": "loud"}, {"jingle_every": 99}):
        with pytest.raises(ValueError):
            profiles.validate([{"name": "A", "artists": ["x"], "settings": bad}])
