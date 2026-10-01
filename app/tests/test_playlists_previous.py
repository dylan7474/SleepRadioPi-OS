"""Playlists (your own lists of tracks), Previous (like a CD player), and the
knob's volume modes."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio import speaker as speaker_mod
from sleepradiopi.audio.speaker import SpeakerControl
from sleepradiopi.broadcast import playlists
from sleepradiopi.broadcast.station import OnAir
from sleepradiopi.io import presets
from sleepradiopi.web.server import make_handler

from test_albums import _on_air
from test_ondemand import _od_station
from test_speaker_and_knob import _speaker

MICHELLE = ["music", "The Beatles/Rubber Soul/02 - Michelle.mp3"]
STORM = ["ondemand", "Thunderstorms/01 - Rain on a tin roof - 1 hour.mp3"]


def _playing(st, track, seconds_in):
    """As if `track` has been on air for this long, in the show."""
    st._track_started(track)
    st.on_air = OnAir("track", track.title, track.artist, started=time.time() - seconds_in)


def test_validate() -> None:
    ok = playlists.validate([{"name": "  Sunday  best ", "tracks": [MICHELLE, STORM]}])
    assert ok == [{"name": "Sunday best", "tracks": [MICHELLE, STORM]}]
    for bad in ([{"name": "", "tracks": []}], [{"name": "A"}, {"name": "a"}],
                [{"name": "A", "tracks": [["elsewhere", "x.mp3"]]}],
                [{"name": "A", "tracks": [["music", "../etc/passwd"]]}], {"name": "A"}):
        with pytest.raises(ValueError):
            playlists.validate(bad)


def test_adding_tracks_folders_and_the_song_playing(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    st.add_to_playlist("Sunday", st.refs_for(root="music", path=MICHELLE[1]))
    st.add_to_playlist("Sunday", st.refs_for(root="ondemand", folder="Old radio shows/Hancock", deep=True))
    st._track_started(st.tracks[0])
    st.add_to_playlist("sunday", st.refs_for(now=True))          # (names match whatever the case)
    p = playlists.find(st.playlists, "Sunday")
    assert len(p["tracks"]) == 5 and p["tracks"][0] == MICHELLE and p["tracks"][1][0] == "ondemand"
    view = st.playlist_view(p)
    assert view["tracks"][0]["title"] == "Michelle" and not any(t["missing"] for t in view["tracks"])
    with pytest.raises(ValueError):
        st.refs_for(root="music", path="Nobody/Nothing.mp3")


def test_play_on_air_starts_at_once_straight_through(tmp_path: Path, monkeypatch) -> None:
    """A playlist is music only (the DJ and jingles are Theme Radio's): it takes
    over at once, like an album, and Stop lets the song playing finish."""
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.add_to_playlist("Mix", [MICHELLE, STORM, ["music", "Nick Drake/Pink Moon/01 - Pink Moon.mp3"]])
    _playing(st, st._take_next(), 30)
    st.play_playlist("Mix")
    assert st.source["kind"] == "playlist" and st._switch.is_set()      # cuts in straight away
    assert st.source["refs"][0] == MICHELLE
    assert st.requests() == []                                         # not through the show's queue
    assert st.playlist_status() == {"name": "Mix", "left": 2, "shuffle": False}
    assert st.stop_playlist() is True
    assert st.playlist_status()["left"] == 0 and st.stop_playlist() is False


def test_play_off_air_is_ready_for_the_power_on(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    st.add_to_playlist("Mix", [MICHELLE, STORM])
    st.play_playlist("Mix")
    assert st.source["kind"] == "playlist" and st.source["name"] == "Mix"


def test_play_while_streaming_replaces_it(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.play_album(root="ondemand", folder="Thunderstorms")
    st.add_to_playlist("Mix", [MICHELLE])
    st.play_playlist("Mix")
    assert st.source["kind"] == "playlist" and st.source["refs"] == [MICHELLE]


def test_shuffle_and_missing_files(tmp_path: Path) -> None:
    st = _od_station(tmp_path)
    songs = [["music", f"The Beatles/Rubber Soul/0{n} - {t}.mp3"]
             for n, t in enumerate(["Girl", "Michelle", "The Word", "Nowhere Man"], 1)]
    st.add_to_playlist("Beatles", songs + [["music", "Gone/Gone/01 - Gone.mp3"]])
    assert st.playlist_view(st.playlists[0])["tracks"][-1]["missing"] is True
    reply = st.play_playlist("Beatles", shuffle=True)
    assert reply["tracks"] == 4                                  # the missing one is skipped
    assert sorted(r for r in map(tuple, st.source["refs"])) == sorted(map(tuple, songs))
    assert st.playlist_status()["shuffle"] is True
    with pytest.raises(ValueError):
        st.play_playlist("Nothing like it")


def test_a_playlist_plays_with_no_dj_or_jingles_then_back_to_the_show(tmp_path: Path, monkeypatch) -> None:
    """The real show loop: only the playlist's songs, nothing in between, then the show."""
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.add_to_playlist("Mix", [MICHELLE, STORM])
    played = []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air, near_end=None, **kw: played.append(on_air.kind + ":" + on_air.title))
    monkeypatch.setattr(st.output, "start", lambda: None)
    monkeypatch.setattr(st.output, "stop", lambda: None)

    def back_to_the_show(back=False):
        played.append("show" + (" (back)" if back else ""))
        st._stop.set()
    monkeypatch.setattr(st, "_run_music", back_to_the_show)
    st.play_playlist("Mix")
    st._run_show()
    assert played == ["track:Michelle", "track:Rain on a tin roof - 1 hour", "show (back)"]
    assert st.source is None and st.playlist_status() is None


def test_a_stopped_playlist_ends_after_the_song_playing(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.add_to_playlist("Mix", [MICHELLE, STORM])
    played = []

    def play_file(path, on_air, near_end=None, **kw):
        played.append(on_air.title)
        st.stop_playlist()
    monkeypatch.setattr(st, "_play_file", play_file)
    st.play_playlist("Mix")
    st._switch.clear()
    st._run_album(st.source)
    assert played == ["Michelle"] and st.source is None


def test_previous_in_a_playlist_steps_back(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.add_to_playlist("Mix", [MICHELLE, STORM])
    st.play_playlist("Mix")
    st.source["track"] = 1
    st.on_air = OnAir("track", "Rain", started=time.time() - 1)
    assert st.previous() is True and st._album_jump == 0


def test_previous_restarts_after_five_seconds(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    first, second = st._take_next(), st._take_next()
    _playing(st, first, 200)
    _playing(st, second, 30)
    assert st.previous() is True and st._jump is second          # started again
    assert st.requests() == []


def test_previous_early_goes_back_then_on_to_the_one_cut_short(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    first, second = st._take_next(), st._take_next()
    _playing(st, first, 200)
    _playing(st, second, 2)
    assert st.previous() is True and st._jump is first
    assert st._queue[0] is second


def test_previous_in_a_gap_replays_the_song_just_ended(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    song = st._take_next()
    _playing(st, song, 200)
    st.on_air = OnAir("dj", "That was…")
    assert st.previous() is True and st._jump is song


def test_previous_in_an_album_steps_back(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.play_album(root="ondemand", folder="Classical/Beethoven Symphony 9")
    st.source["track"] = 2
    st.on_air = OnAir("track", "Adagio", started=time.time() - 1)
    assert st.previous() is True and st._album_jump == 1
    st.on_air = OnAir("track", "Adagio", started=time.time() - 60)
    assert st.previous() is True and st._album_jump == 2


def test_previous_has_nothing_to_do_on_a_station_or_off_air(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    assert st.previous() is False                                # off air
    _on_air(st, monkeypatch)
    st.on_air = OnAir("radio", "BBC Radio 4")
    assert st.previous() is False


def test_playlist_on_a_button() -> None:
    p = presets.validate({"kind": "playlist", "name": "Sunday", "shuffle": True})
    assert p == {"kind": "playlist", "name": "Sunday", "shuffle": True}
    assert presets.label(p) == "Sunday (shuffled)"
    assert presets.same(p, {"kind": "playlist", "name": "sunday", "shuffle": True})
    assert not presets.same(p, {**p, "shuffle": False})


def test_knob_modes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(speaker_mod, "SAVE_AFTER_S", 0.05)
    spk, _ = _speaker(tmp_path)
    ctl = SpeakerControl(spk, lambda: None, lambda: None, default_volume=40)
    ctl.knob(1, now=10.0)                         # slow: a step a click
    ctl.knob(1, now=11.0)
    assert spk.volume == 42
    ctl.knob(1, now=11.03)                        # a quick spin: bigger steps
    ctl.knob(1, now=11.06)
    assert spk.volume == 52
    ctl.knob(-1, now=11.08)                       # turned back: slow again
    assert spk.volume == 51
    for mode, per in (("fine", 1), ("normal", 2), ("coarse", 5)):
        ctl.set_knob_mode(mode)
        before = spk.volume
        ctl.knob(-1, now=20.0)
        ctl.knob(-1, now=20.01)
        assert spk.volume == before - 2 * per
    with pytest.raises(ValueError):
        ctl.set_knob_mode("turbo")


def test_web_api(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        song = req("/api/search?q=michelle")["results"][0]
        assert song["path"] == MICHELLE[1]
        got = req("/api/playlists/add", {"name": "Sunday", "root": "music", "path": song["path"]})
        assert got["added"] == 1 and got["playlists"][0]["tracks"][0]["title"] == "Michelle"
        got = req("/api/playlists/add", {"name": "Sunday", "root": "ondemand", "folder": "Thunderstorms"})
        assert got["tracks"] == 2
        req("/api/playlists", {"playlists": [{"name": "Sunday", "tracks": [STORM]}]})   # edited on the page
        assert req("/api/playlists")["playlists"][0]["tracks"][0]["path"] == STORM[1]
        assert req("/api/playlists/play", {"name": "Sunday"})["tracks"] == 1
        assert req("/api/previous", {})["previous"] is False                            # (off air)
        with pytest.raises(urllib.error.HTTPError):
            req("/api/playlists/play", {"name": "Nope"})
    finally:
        httpd.shutdown()


def test_the_show_loop_goes_back_with_no_gap_then_carries_on(tmp_path: Path, monkeypatch) -> None:
    """The real show loop: Previous two seconds into the second song plays the
    first again straight away (no DJ gap), then the second, then on."""
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    first = st._take_next()
    events = []

    def play_track(track):
        st._track_started(track)
        st.on_air = OnAir("track", track.title, started=time.time() - 2)
        events.append(track.title)
        if len([e for e in events if e != "gap"]) == 2:
            assert st.previous()
        if len([e for e in events if e != "gap"]) >= 5:
            st._stop.set()
    monkeypatch.setattr(st, "_play_track", play_track)
    monkeypatch.setattr(st, "_run_gap", lambda: events.append("gap"))
    monkeypatch.setattr(st, "_take_opening", lambda: ([], first))
    st._run_music_show()
    songs = [e for e in events if e != "gap"]
    assert songs[2] == songs[0] and songs[3] == songs[1]
    assert events[:5] == [songs[0], "gap", songs[1], songs[0], "gap"]   # no gap before the replay


def test_renaming_a_playlist_takes_its_buttons_and_the_one_playing_with_it(tmp_path: Path) -> None:
    from sleepradiopi.io.presets import Presets
    st = _od_station(tmp_path)
    st.add_to_playlist("New playlist", [MICHELLE])
    st.add_to_playlist("Sunday", [STORM])
    config = tmp_path / "config.json"
    config.write_text("{}")
    presets = Presets(st, None, config, [{"kind": "playlist", "name": "New playlist"}, None])
    st.play_playlist("New playlist")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, config, presets=presets))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def rename(old, new):
        r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/playlists/rename",
                                   data=json.dumps({"old": old, "new": new}).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(r).read())
    try:
        got = rename("new playlist", "  Dad's  favourites ")
        assert got["name"] == "Dad's favourites"
        assert [p["name"] for p in got["playlists"]] == ["Dad's favourites", "Sunday"]
        with pytest.raises(urllib.error.HTTPError):
            rename("Dad's favourites", "sunday")                     # taken
        with pytest.raises(urllib.error.HTTPError):
            rename("Nobody", "X")
    finally:
        httpd.shutdown()
    assert st.playlist_status()["name"] == "Dad's favourites"           # the one playing too
    assert presets.banks["day"][0]["name"] == "Dad's favourites"
    assert json.loads(config.read_text())["playlists"][0]["name"] == "Dad's favourites"


def test_a_playlist_song_not_measured_yet_plays_at_once(tmp_path: Path, monkeypatch) -> None:
    """Its loudness scan is a whole decode (tens of seconds for a long track on
    a Zero): a playlist doesn't wait for it -- the song plays as it is."""
    from concurrent.futures import Future
    from sleepradiopi.broadcast import station as station_mod
    st = _od_station(tmp_path)
    monkeypatch.setattr(st, "_write", lambda block: None)
    monkeypatch.setattr(station_mod, "SOURCE_SCAN_WAIT_S", 0.05)
    monkeypatch.setattr(st, "_scan", lambda path: Future())          # (never finishes)
    decoded = []
    monkeypatch.setattr(station_mod.pcm, "decode", lambda path, a, b: decoded.append(path.name) or iter(()))
    st.add_to_playlist("Mix", [MICHELLE])
    st.play_playlist("Mix")
    st._switch.clear()
    t0 = time.monotonic()
    st._run_album(st.source)
    assert decoded == ["02 - Michelle.mp3"] and time.monotonic() - t0 < 2


def _run_source(st, monkeypatch):
    played, paused = [], []
    monkeypatch.setattr(st, "_play_file", lambda path, on_air, near_end=None, **kw: played.append(on_air.title))
    st.on_book_end = lambda: paused.append(True)
    st._switch.clear()
    st._run_album(st.source)
    return played, paused


def test_a_song_dropped_on_the_radio_plays_alone_then_it_pauses(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.play_track("music", MICHELLE[1], then="pause")
    assert st.source["kind"] == "playlist" and st.source["refs"] == [MICHELLE]
    played, paused = _run_source(st, monkeypatch)
    assert played == ["Michelle"] and paused == [True] and st.source is None


def test_an_artist_dropped_plays_their_songs_shuffled_once_then_pauses(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.play_album(root="music", folder="The Beatles", deep=True, shuffle=True, then="pause")
    assert st.source["kind"] == "playlist" and st.source["shuffle"] is True
    played, paused = _run_source(st, monkeypatch)
    assert sorted(played) == ["Girl", "Michelle", "Nowhere Man", "The Word"] and paused == [True]


def test_an_album_from_a_button_still_goes_back_to_the_show(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    _on_air(st, monkeypatch)
    st.play_album(root="music", folder="The Beatles/Rubber Soul")
    assert "then" not in st.source
    played, paused = _run_source(st, monkeypatch)
    assert len(played) == 4 and paused == [] and st.source is None


def test_web_play_passes_then_pause(tmp_path: Path, monkeypatch) -> None:
    st = _od_station(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(path, body):
        r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", data=json.dumps(body).encode(),
                                   method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        req("/api/album/play", {"root": "music", "folder": "The Beatles", "deep": True, "shuffle": True, "then": "pause"})
        assert st.source["kind"] == "playlist" and st.source["then"] == "pause"
        req("/api/track/play", {"root": "music", "path": MICHELLE[1], "then": "pause"})
        assert st.source["refs"] == [MICHELLE] and st.source["then"] == "pause"
        st.add_to_playlist("Mix", [MICHELLE])
        req("/api/playlists/play", {"name": "Mix", "then": "pause"})
        assert st.source["name"] == "Mix" and st.source["then"] == "pause"
        req("/api/album/play", {"root": "music", "folder": "The Beatles/Rubber Soul"})
        assert st.source["kind"] == "album" and "then" not in st.source
    finally:
        httpd.shutdown()
