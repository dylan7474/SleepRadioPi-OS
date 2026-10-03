import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio.eq import Equalizer
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load
from sleepradiopi.web import server as server_mod
from sleepradiopi.web.server import make_handler


class FakeStation:
    def status(self):
        return {"on_air": False}

    def set_messages(self, messages): ...        # (a whole saved file sets these too)


def _file(**settings):
    return {"format": backup.FORMAT, "version": 1, "settings": settings, "volume": None}


def test_export_leaves_out_this_radios_own_setup(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music", "http_port": 80,
                                "news_enabled": False, "speaker_mono": True}))
    data = backup.export(conf, 47)
    assert data["format"] == backup.FORMAT and data["volume"] == 47
    s = data["settings"]
    assert s["news_enabled"] is False and s["speaker_mono"] is True
    assert s["broadcast_chattiness"] == "maximum"          # defaults are included
    assert not backup.LOCAL & s.keys()


@pytest.mark.parametrize("bad", [
    [], {"format": "something else"}, {"format": backup.FORMAT, "version": 99, "settings": {}},
    _file(news_enabled="yes"), _file(broadcast_jingle_every=0), _file(news_speed=float("nan")),
    _file(broadcast_voice="robot"), _file(speaker_eq={"bass": 40}), _file(speaker_eq={"loud": 1}),
    _file(speaker_volume=True), {**_file(), "volume": 101},
])
def test_parse_rejects_bad_files(bad) -> None:
    with pytest.raises(backup.BadSettings):
        backup.parse(bad)


def test_parse_skips_unknown_and_local_keys() -> None:
    settings, volume = backup.parse({**_file(news_speed=1, music_folder="/etc", future=1), "volume": 5})
    assert settings == {"news_speed": 1} and volume == 5


def test_apply_keeps_the_local_setup_and_reports_changes(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music", "news_enabled": True}))
    changed = backup.apply(conf, {"news_enabled": False, "broadcast_chattiness": "maximum"})
    assert changed == {"news_enabled"}                     # maximum was already the default
    saved = json.loads(conf.read_text())
    assert saved["music_folder"] == "/media/music" and saved["news_enabled"] is False
    assert load(conf).news_enabled is False


def test_round_trip_to_a_second_radio(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"news_speed": 0.9, "speaker_eq": {"bass": 4, "mid": 0, "treble": -2},
                             "music_folder": "/a"}))
    b.write_text(json.dumps({"music_folder": "/b"}))
    settings, _ = backup.parse(json.loads(json.dumps(backup.export(a, 30))))
    backup.apply(b, settings)
    got = load(b)
    assert got.news_speed == 0.9 and got.speaker_eq["bass"] == 4 and got.music_folder == "/b"


@pytest.fixture
def radio(tmp_path: Path, monkeypatch):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"music_folder": "/media/music"}))
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(44100, 2))
    ctl = SpeakerControl(spk, lambda: None, lambda: None, state_file=tmp_path / "speaker.json",
                         config_file=conf)
    restarts = []
    monkeypatch.setattr(server_mod, "_restart_soon", lambda speaker: restarts.append(1))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", ctl, conf, restarts
    httpd.shutdown()


def _load(base, body: bytes):
    req = urllib.request.Request(base + "/api/settings", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_web_save_is_a_download(radio) -> None:
    base, ctl, _, _ = radio
    ctl.set_volume(33)
    with urllib.request.urlopen(base + "/api/settings") as r:
        assert "attachment" in r.headers["Content-Disposition"]
        assert "sleepradio-settings-" in r.headers["Content-Disposition"]
        data = json.load(r)
    assert data["volume"] == 33 and "music_folder" not in data["settings"]


def test_web_load_applies_live_settings_without_a_restart(radio, monkeypatch) -> None:
    base, ctl, conf, restarts = radio
    monkeypatch.setenv(server_mod.RESTART_ENV, "1")
    body = {**_file(speaker_mono=True, speaker_eq={"bass": 5, "mid": 0, "treble": 0}), "volume": 20}
    reply = _load(base, json.dumps(body).encode())
    assert reply["ok"] and not reply["needs_restart"] and not restarts
    assert reply["changed"] == ["speaker_eq", "speaker_mono", "volume"]
    assert ctl.speaker.mono and ctl.speaker.eq.gains["bass"] == 5 and ctl.volume == 20
    assert json.loads((conf.parent / "speaker.json").read_text())["volume"] == 20


def test_web_load_restarts_only_when_supervised(radio, monkeypatch) -> None:
    base, _, conf, restarts = radio
    monkeypatch.delenv(server_mod.RESTART_ENV, raising=False)
    reply = _load(base, json.dumps(_file(broadcast_announcer_volume=0.5)).encode())   # needs a restart
    assert reply["needs_restart"] and not reply["restarting"] and not restarts
    monkeypatch.setenv(server_mod.RESTART_ENV, "1")
    reply = _load(base, json.dumps(_file(news_quiet_hours=False)).encode())
    # the restart is requested just after the reply is sent: wait for it
    deadline = time.monotonic() + 2
    while not restarts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reply["restarting"] and restarts == [1]
    saved = json.loads(conf.read_text())
    assert saved["broadcast_announcer_volume"] == 0.5 and saved["music_folder"] == "/media/music"


def test_web_load_rejects_a_bad_file_and_changes_nothing(radio) -> None:
    base, ctl, conf, restarts = radio
    before = conf.read_text()
    for body in (b"not json", json.dumps(_file(speaker_mono=True, news_speed=99)).encode(),
                 b"{" + b" " * (server_mod.MAX_SETTINGS_BYTES + 10) + b"}"):
        with pytest.raises(urllib.error.HTTPError) as err:
            _load(base, body)
        assert err.value.code == 400 and "error" in json.load(err.value)
    assert conf.read_text() == before and not ctl.speaker.mono and not restarts


def test_the_radio_keeps_saved_settings_and_loads_them_back(radio) -> None:
    """The desktop's Backups folder: save now, list, load (undoable), take one to the PC, delete."""
    base, ctl, conf, restarts = radio

    def call(path, body=None, raw=None):
        req = urllib.request.Request(base + path, data=raw if raw is not None else None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None and raw is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    assert call("/api/backups")[1]["backups"] == []
    ctl.set_volume(40)
    code, d = call("/api/backups/save", {"name": "Quiet evenings"})
    assert code == 200 and d["name"] == "Quiet evenings" and [b["name"] for b in d["backups"]] == ["Quiet evenings"]
    assert call("/api/backups/save", {"name": "Quiet evenings"})[1]["name"] == "Quiet evenings (2)"   # (never over another)
    assert call("/api/backups/save", {})[1]["name"][:2] == "20"                                       # (no name: the date)
    for bad in ("../x", ".hidden", "a/b", "x" * 61):
        assert call("/api/backups/save", {"name": bad})[0] == 400
    ctl.set_volume(70)
    ctl.set_mono(True)
    code, d = call("/api/backups/load", {"name": "Quiet evenings"})
    assert code == 200 and ctl.speaker.volume == 40 and not ctl.speaker.mono and not restarts
    assert backup.BEFORE in [b["name"] for b in call("/api/backups")[1]["backups"]]
    call("/api/backups/load", {"name": backup.BEFORE})                 # ...undone: as it was before the load
    assert ctl.speaker.volume == 70 and ctl.speaker.mono
    code, data = call("/api/backups/file?name=Quiet%20evenings")
    assert code == 200 and data["volume"] == 40
    code, d = call("/api/backups/upload?name=From%20the%20PC", raw=json.dumps(data).encode())
    assert code == 200 and d["name"] == "From the PC" and ctl.speaker.volume == 70      # (kept, not loaded)
    assert call("/api/backups/upload?name=Junk", raw=b"not json")[0] == 400
    assert call("/api/backups/delete", {"name": "From the PC"})[0] == 200
    assert call("/api/backups/delete", {"name": "From the PC"})[0] == 400
    assert call("/api/backups/load", {"name": "Never saved"})[0] == 400
    assert "From the PC" not in [b["name"] for b in call("/api/backups")[1]["backups"]]
    store = backup.Store(conf.parent / "backups")
    for i in range(backup.MAX_KEPT):
        try:
            store.save(data, f"Copy {i}")
        except backup.BadSettings as e:
            assert "delete one first" in str(e)
            break
    else:
        assert False, "no limit"
    assert len(store.list()) == backup.MAX_KEPT
