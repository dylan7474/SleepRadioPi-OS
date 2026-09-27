import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.config.settings import load
from sleepradiopi.web.server import make_handler


class FakeStation:
    def status(self):
        return {"on_air": False}


def _control(tmp_path: Path, conf: Path) -> SpeakerControl:
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    return SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)


def test_set_mono_switches_at_once_and_keeps_the_rest_of_the_config(tmp_path: Path) -> None:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"speaker_enabled": True, "speaker_mono": False, "some_future_key": 7}))
    ctl = _control(tmp_path, conf)
    ctl.set_mono(True)
    assert ctl.speaker.mono and ctl.status()["mono"] is True
    saved = json.loads(conf.read_text())
    assert saved == {"speaker_enabled": True, "speaker_mono": True, "some_future_key": 7}
    assert load(conf).speaker_mono is True      # what the next start-up reads
    ctl.set_mono(False)
    assert not ctl.speaker.mono and json.loads(conf.read_text())["speaker_mono"] is False


@pytest.fixture
def server(tmp_path: Path):
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    ctl = _control(tmp_path, conf)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", ctl, conf
    httpd.shutdown()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_web_api_sets_mono_and_status_shows_it(server) -> None:
    base, ctl, conf = server
    assert _post(base + "/api/speaker", {"mono": True})["mono"] is True
    with urllib.request.urlopen(base + "/api/status") as r:
        assert json.load(r)["speaker"]["mono"] is True
    assert json.loads(conf.read_text())["speaker_mono"] is True
    assert _post(base + "/api/speaker", {"mono": False})["mono"] is False


def test_web_api_rejects_a_non_boolean_mono(server) -> None:
    base, ctl, _ = server
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(base + "/api/speaker", {"mono": "yes"})
    assert err.value.code == 400 and not ctl.speaker.mono


def test_web_api_sets_the_eq(tmp_path: Path) -> None:
    from sleepradiopi.audio.eq import Equalizer
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"], eq=Equalizer(44100, 2))
    ctl = SpeakerControl(spk, lambda: None, lambda: None, config_file=conf)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _post(base + "/api/speaker", {"eq": {"bass": 3, "treble": -2}})["eq"] == \
            {"bass": 3, "mid": 0, "treble": -2}
        with urllib.request.urlopen(base + "/api/status") as r:
            assert json.load(r)["speaker"]["eq"]["bass"] == 3
        assert json.loads(conf.read_text())["speaker_eq"]["treble"] == -2
        for bad in ({"eq": {"volume": 3}}, {"eq": {"bass": "loud"}}, {"eq": 5},
                    {"eq": {"bass": True}}):
            with pytest.raises(urllib.error.HTTPError) as err:
                _post(base + "/api/speaker", bad)
            assert err.value.code == 400
        req = urllib.request.Request(base + "/api/speaker", data=b'{"eq": {"bass": Infinity}}',
                                     method="POST", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 400
    finally:
        httpd.shutdown()
