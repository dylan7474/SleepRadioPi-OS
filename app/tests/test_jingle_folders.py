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
    assert st.profile == "Carisbrooke"                                         # no default station: the first theme
    assert sorted(j.path.name for j in st.jingles) == ["Cari 1.mp3", "Cari 2.mp3"]
    assert st.builder.station == "Carisbrooke Radio"
    st.set_profiles([{"name": "Carisbrooke", "artists": ["Bread"]}, {"name": "Classical", "artists": ["ABBA"]}])
    st.set_profile("Classical")
    assert st.jingles == []                                                    # no folder / empty: none
    st.set_artist("ABBA")
    assert st.jingles == []                                                    # artist radio: none of its own
    st.set_artist(None)                                                        # back: the theme last played
    assert st.profile == "Classical"


def test_the_start_up_jingle_is_the_stations_own_else_defaults(tmp_path: Path) -> None:
    st = _station(tmp_path, {"Default": ["Start.mp3"]})                      # (on Carisbrooke: no jingles of its own)
    pending = Future()
    welcome = station_mod.Step("say", station_mod.Speech("Good evening", "stock", pending))
    steps = st._opening_steps([welcome], "power-on")
    assert [(s.kind, s.jingle.path.name) for s in steps] == [("jingle", "Start.mp3")] and pending.cancelled()
    done = Future()
    done.set_result(None)
    ready = station_mod.Step("say", station_mod.Speech("Good evening", "stock", done))
    steps = st._opening_steps([ready], "power-on")
    assert steps[0].jingle.path.name == "Start.mp3" and steps[1] is ready   # then the welcome
    (tmp_path / "jingles" / "Carisbrooke").mkdir()
    (tmp_path / "jingles" / "Carisbrooke" / "Cari.mp3").write_bytes(b"x")
    st._load_jingles(force=True)
    steps = st._opening_steps([station_mod.Step("say", station_mod.Speech("Hi", "stock", Future()))], "power-on")
    assert steps[0].jingle.path.name == "Cari.mp3"                            # the station's own first


def test_the_folders_are_made_and_loose_jingles_go_into_defaults(tmp_path: Path) -> None:
    from sleepradiopi.main import jingle_folders
    st = _station(tmp_path, {"": ["Old 1.mp3", "Old 2.mp3"]})
    rescans = []
    media = MediaLibrary({"jingles": tmp_path / "jingles"}, on_changed=lambda kinds: rescans.append(kinds),
                         lease=tmp_path / "run" / "media-rw")
    jingle_folders(media, st)
    root = tmp_path / "jingles"
    assert {p.name for p in root.iterdir()} == {"Default", "Carisbrooke"}
    assert sorted(p.name for p in (root / "Default").iterdir()) == ["Old 1.mp3", "Old 2.mp3"]
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
    st.set_artist("ABBA")                                   # artist radio: all the radio's
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


def test_renaming_a_theme_takes_its_things_with_it(tmp_path: Path) -> None:
    """Its jingles folder, its messages, buttons and programmes that play it, the
    station playing, and everything saved."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from sleepradiopi.io.presets import Presets
    from sleepradiopi.web.server import make_handler
    st = _station(tmp_path, {"Carisbrooke": ["Cari.mp3"], "Sleep Radio": ["Main.mp3"]})
    st.messages.set({"list": [{"text": "Lunch soon", "station": "Carisbrooke"}, {"text": "Hello"}]})
    st.set_profile("Carisbrooke")
    config = tmp_path / "config.json"
    config.write_text("{}")
    presets = Presets(st, None, config, [{"kind": "show", "profile": "Carisbrooke"}, None])
    media = MediaLibrary({"jingles": tmp_path / "jingles"}, on_changed=st.reload_library, lease=tmp_path / "run" / "media-rw")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, config, presets=presets, media=media))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/profiles/rename",
                                   data=json.dumps({"old": "Carisbrooke", "new": "  Carisbrooke  Lodge "}).encode(),
                                   method="POST", headers={"Content-Type": "application/json"})
        got = json.loads(urllib.request.urlopen(r).read())
    finally:
        httpd.shutdown()
    assert got["name"] == "Carisbrooke Lodge"
    assert [p["name"] for p in st.profiles] == ["Carisbrooke Lodge"]
    assert st.profile == "Carisbrooke Lodge" and st.builder.station == "Carisbrooke Lodge Radio"
    assert (tmp_path / "jingles" / "Carisbrooke Lodge" / "Cari.mp3").exists() and not (tmp_path / "jingles" / "Carisbrooke").exists()
    assert [j.path.name for j in st.jingles] == ["Cari.mp3"]                 # still its jingles
    assert st.messages.cfg["list"][0]["station"] == "Carisbrooke Lodge" and "station" not in st.messages.cfg["list"][1]
    assert presets.banks["day"][0]["profile"] == "Carisbrooke Lodge"
    saved = json.loads(config.read_text())
    assert saved["broadcast_profile"] == "Carisbrooke Lodge" and saved["profiles"][0]["name"] == "Carisbrooke Lodge"
    assert saved["messages"]["list"][0]["station"] == "Carisbrooke Lodge"
    with pytest.raises(ValueError):
        st.rename_profile("Nobody", "X")


def test_there_is_always_a_default_theme(tmp_path: Path) -> None:
    """Made if it's missing -- the Sleep Radio theme of all my music made before it
    becomes it, jingles folder and buttons too -- messages with no theme are its, and
    it can't be renamed or deleted."""
    import json
    from sleepradiopi.io.presets import Presets
    from sleepradiopi.main import ensure_default_theme
    st = _station(tmp_path, {"Sleep Radio": ["Main.mp3"]})
    st.set_profiles([{"name": "Sleep Radio", "artists": [], "all": True}, {"name": "Carisbrooke", "artists": ["Bread"]}])
    st.set_profile("Sleep Radio")
    st.messages.set({"list": [{"text": "For Dad"}, {"text": "Cari", "station": "Carisbrooke"}], "date_first": False})
    config = tmp_path / "config.json"
    config.write_text("{}")
    presets = Presets(st, None, config, [{"kind": "show", "profile": "Sleep Radio"}, None])
    media = MediaLibrary({"jingles": tmp_path / "jingles"}, on_changed=st.reload_library, lease=tmp_path / "run" / "media-rw")
    assert ensure_default_theme(st, config, media, presets) and not ensure_default_theme(st, config, media, presets)
    assert [p["name"] for p in st.profiles] == ["Default", "Carisbrooke"] and st.profile == "Default"
    assert (tmp_path / "jingles" / "Default" / "Main.mp3").exists() and presets.banks["day"][0]["profile"] == "Default"
    assert [m.get("station") for m in st.messages.cfg["list"]] == ["Default", "Carisbrooke"]
    saved = json.loads(config.read_text())
    assert saved["broadcast_profile"] == "Default" and saved["messages"]["list"][0]["station"] == "Default"
    for old, new in (("Default", "Home"), ("Carisbrooke", "default")):
        with pytest.raises(ValueError, match="always there"):
            st.rename_profile(old, new)
    st.set_artist("ABBA")
    st.set_artist(None)
    assert st.profile == "Default"                                           # (Default played last)
    fresh = _station(tmp_path / "new", {})
    fresh.profiles = []
    assert ensure_default_theme(fresh, None) and fresh.profiles[0] == {"name": "Default", "artists": [], "all": True}
