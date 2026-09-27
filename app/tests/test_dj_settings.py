import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.config.settings import load
from sleepradiopi.web import server as server_mod
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station


def _dj_station(tmp_path: Path):
    voices = tmp_path / "voices"
    for v in ("personal", "stock"):
        (voices / v).mkdir(parents=True)
        (voices / v / "model.onnx").write_bytes(b"x")
    (voices / "not-a-voice").mkdir()
    hooks = tmp_path / "hooks.txt"
    hooks.write_text("Keep it groovy.\nStay cool, cats.\n")
    jingles = tmp_path / "jingles"
    jingles.mkdir()
    (jingles / "sleep_radio.mp3").write_bytes(b"x")
    st = _station(tmp_path, voices_dir=voices, hooks_file=str(hooks), broadcast_dj_hooks=False,
                  broadcast_jingle_enabled=False)
    st.jingles_dir = jingles
    return st


def test_dj_settings_report_what_there_is(tmp_path: Path) -> None:
    st = _dj_station(tmp_path)
    got = st.dj_settings()
    assert got["voices"] == ["personal", "stock"] and got["voice"] == "stock"
    assert got["chattiness"] == "maximum" and "minimal" in got["chattiness_options"]
    assert got["dj_hooks"] is False and got["hooks_available"] is True
    assert got["jingle_every"] == 0 and got["jingles_available"] is True


def test_changes_apply_live(tmp_path: Path) -> None:
    st = _dj_station(tmp_path)
    st.set_dj(chattiness="minimal", dj_hooks=True, jingle_every=3, news_enabled=False)
    assert st.config.tracks_per_link == 5 and not st.config.announce_every_track
    assert st._show_clock.config is st.config                 # the show clock sees it
    assert st.builder.hooks is not None
    assert st.config.jingle_every == 3 and len(st.jingles) == 1   # found the jingles now
    assert st.config.news_enabled is False
    st.set_dj(chattiness="maximum", dj_hooks=False, jingle_every=0)
    assert st.config.announce_every_track and st.builder.hooks is None and st.config.jingle_every == 0
    with pytest.raises(ValueError):
        st.set_dj(chattiness="shouty")
    with pytest.raises(ValueError):
        st.set_dj(jingle_every=99)


@pytest.fixture
def radio(tmp_path: Path, monkeypatch):
    st = _dj_station(tmp_path)
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music"}))
    restarts = []
    monkeypatch.setattr(server_mod, "_restart_soon", lambda speaker: restarts.append(1))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(body=None):
        r = urllib.request.Request(base + "/api/dj", data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    yield req, st, conf, restarts
    httpd.shutdown()


def test_web_sets_and_saves_the_dj(radio) -> None:
    req, st, conf, restarts = radio
    assert req()["voices"] == ["personal", "stock"]
    got = req({"chattiness": "chatty", "dj_hooks": True, "jingle_every": 6, "news_enabled": False})
    assert got["chattiness"] == "chatty" and got["dj_hooks"] and got["jingle_every"] == 6
    saved = load(conf)
    assert (saved.broadcast_chattiness, saved.broadcast_dj_hooks, saved.broadcast_jingle_enabled,
            saved.broadcast_jingle_every, saved.news_enabled) == ("chatty", True, True, 6, False)
    req({"jingle_every": 0})
    assert load(conf).broadcast_jingle_enabled is False and load(conf).broadcast_jingle_every == 6
    assert json.loads(conf.read_text())["music_folder"] == "/media/music" and not restarts


def test_web_voice_change_restarts_only_when_supervised(radio, monkeypatch) -> None:
    req, st, conf, restarts = radio
    monkeypatch.delenv(server_mod.RESTART_ENV, raising=False)
    got = req({"voice": "personal"})
    assert got["saved_voice"] == "personal" and not got["restarting"] and not restarts
    assert load(conf).broadcast_voice == "personal"
    monkeypatch.setenv(server_mod.RESTART_ENV, "1")
    assert req({"voice": "personal"})["restarting"]            # still differs from the running voice
    deadline = __import__("time").monotonic() + 2
    while not restarts and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert restarts == [1]


@pytest.mark.parametrize("bad", [{"voice": "robot"}, {"chattiness": "shouty"}, {"dj_hooks": "yes"},
                                 {"jingle_every": "4"}, {"jingle_every": 99}, {"news_enabled": 1}])
def test_web_rejects_bad_values(radio, bad) -> None:
    req, *_ = radio
    with pytest.raises(urllib.error.HTTPError) as err:
        req(bad)
    assert err.value.code == 400 and "error" in json.load(err.value)
