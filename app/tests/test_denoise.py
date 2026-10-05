"""Noise reduction for receivers (audio/denoise.py)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import denoise, pcm
from sleepradiopi.broadcast.station import _is_receiver
from sleepradiopi.config.settings import load
from sleepradiopi.web.server import make_handler

RATE = pcm.SAMPLE_RATE


def _played(d, x, size=4096):                      # (as the station gives it: a block at a time)
    return np.concatenate([d.process(x[i:i + size]) for i in range(0, len(x), size)])


def _rms(x) -> float:
    return float(np.sqrt(np.mean(x.astype(float) ** 2)))


def test_the_hiss_goes_down_and_the_voice_stays() -> None:
    rng = np.random.default_rng(1)
    t = np.arange(RATE * 8) / RATE
    hiss = rng.standard_normal(len(t)) * 1500
    # a "voice": a few tones that come and go, a syllable at a time
    said = (np.sin(2 * np.pi * 3 * t) > 0.2) * (np.sin(2 * np.pi * 400 * t) + 0.6 * np.sin(2 * np.pi * 1100 * t) + 0.4 * np.sin(2 * np.pi * 2300 * t)) * 5000
    x = np.repeat(np.clip(hiss + said, -32768, 32767).astype(np.int16)[:, None], pcm.CHANNELS, axis=1)
    speaking = (np.sin(2 * np.pi * 3 * t) > 0.5)
    gaps = (np.sin(2 * np.pi * 3 * t) < -0.3)
    left = {}
    for level in ("light", "strong"):
        y = _played(denoise.Denoiser(level), x)
        assert y.dtype == np.int16 and y.shape[1] == pcm.CHANNELS and 0 <= len(x) - len(y) < denoise.FRAME     # the same, half a frame late
        assert np.array_equal(y[:, 0], y[:, 1])
        late = denoise.FRAME // 2
        was, now = x[late * 0:len(y), 0], y[:, 0]
        keep = np.arange(len(now)) > RATE * 4                                       # (once it has learnt the noise)
        down = 20 * np.log10(_rms(was[gaps[:len(now)] & keep]) / _rms(now[gaps[:len(now)] & keep]))
        voice = 20 * np.log10(_rms(was[speaking[:len(now)] & keep]) / _rms(now[speaking[:len(now)] & keep]))
        left[level] = down
        assert voice < 2.0, (level, voice)                                           # the voice is all but untouched
    assert 5 < left["light"] < 10 and 12 < left["strong"] < 19                       # the hiss between words: down, to a floor, never gone


def test_blocks_of_any_size_and_one_channel() -> None:
    rng = np.random.default_rng(2)
    x = (rng.standard_normal(RATE) * 3000).astype(np.int16)
    whole = denoise.Denoiser("light").process(x)
    pieces = _played(denoise.Denoiser("light"), x, size=333)
    assert whole.ndim == 1 and np.array_equal(whole, pieces)                         # however it's cut up
    assert len(denoise.Denoiser("light").process(x[:100])) == 0                      # (less than half a frame: kept for next time)
    assert denoise.Denoiser("light").process(np.zeros((0, 2), dtype=np.int16)).shape == (0, 2)


def test_the_switch_follows_the_setting(monkeypatch) -> None:
    rng = np.random.default_rng(3)
    x = np.repeat((rng.standard_normal(RATE * 4) * 3000).astype(np.int16)[:, None], 2, axis=1)
    sw = denoise.Switch()
    monkeypatch.setattr(denoise, "LEVEL", "off")
    assert sw.process(x) is x                                                        # off: the block itself
    monkeypatch.setattr(denoise, "LEVEL", "strong")
    assert _rms(sw.process(x)[-RATE:]) < _rms(x) * 0.3                               # steady noise: well down
    monkeypatch.setattr(denoise, "LEVEL", "light")
    light = sw.process(x)
    assert _rms(x) * 0.3 < _rms(light[-RATE:]) < _rms(x) * 0.6                        # a change of level: started afresh
    monkeypatch.setattr(denoise, "LEVEL", "off")
    assert sw.process(x) is x
    assert denoise.clean_level(None) == "off" and denoise.clean_level("Strong") == "strong"
    with pytest.raises(ValueError):
        denoise.clean_level("loud")


def test_it_is_for_receivers_whatever_kind() -> None:
    for url in ("http://rx.local:8074/audio?band=2m", "http://rx.local:8074/audio?band=fm&hold=96.6", "http://rx.local:8074/audio?freq=145.5",
                "http://kiwi.example:8073/?f=7150.00lsb", "http://owrx.example:8073/#freq=145500000,mod=nfm"):
        assert _is_receiver(url), url
    for url in ("http://stream.example/live.mp3", "ysf://a.example:42000/A", "fcs://FCS00290/X"):
        assert not _is_receiver(url), url


def test_the_setting_from_the_web(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(denoise, "LEVEL", "off")
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/rx/nr", {"level": "strong"}) == (200, {"nr": "strong"}) and denoise.LEVEL == "strong" and load(conf).noise_reduction == "strong"
        for bad in ({"level": "loud"}, {"level": 3}, {}):
            assert call("/api/rx/nr", bad)[0] == 400 and denoise.LEVEL == "strong"
        assert call("/api/rx/nr", {"level": "off"}) == (200, {"nr": "off"}) and load(conf).noise_reduction == "off"
    finally:
        httpd.shutdown()
        httpd.server_close()
