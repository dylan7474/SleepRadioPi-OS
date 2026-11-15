import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np

from sleepradiopi.audio import spectrum
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput
from sleepradiopi.web.server import make_handler

RATE = 44_100


def _sine(hz, amp, n=spectrum.SCOPE_N):
    return amp * np.sin(2 * np.pi * hz * np.arange(n) / RATE)


def test_a_tone_lands_in_its_band_at_its_level() -> None:
    db = spectrum.bands(_sine(1000, 1.0), RATE)
    i = spectrum.CENTRES.index(1000)
    assert abs(db[i]) < 0.5                                    # a full-scale sine: 0 dB
    assert all(v < db[i] - 30 for k, v in enumerate(db) if abs(k - i) > 1)
    half = spectrum.bands(_sine(1000, 0.5), RATE)
    assert abs(half[i] - (-6.0)) < 0.5
    low = spectrum.bands(_sine(40, 0.5), RATE)                  # the lowest bands resolve too
    assert int(np.argmax(low)) == spectrum.CENTRES.index(40)
    assert spectrum.bands(np.zeros(spectrum.SCOPE_N), RATE) == [spectrum.FLOOR_DB] * len(spectrum.CENTRES)
    assert len(spectrum.CENTRES) == 28


def test_the_speaker_keeps_samples_only_while_someone_looks(tmp_path) -> None:
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    spk.volume = 50
    spk.start()
    tone = (np.repeat(_sine(2000, 0.25, 4 * spectrum.SCOPE_N)[:, None], 2, axis=1) * 32767).astype(np.int16)
    spk.write(tone)
    assert len(spk._scope) == 0                                 # nobody looking: nothing kept
    first = spk.spectrum()                                      # a look starts it...
    assert not first["live"]
    spk.write(tone)
    d = spk.spectrum()                                          # ...and the next audio is there
    assert d["live"] and d["hz"][int(np.argmax(d["db"]))] == 2000
    assert -14 < max(d["db"]) < -10                             # a quarter of full scale: -12 dB, whatever the volume
    spk.stop()


def test_web_api_gives_the_bands() -> None:
    class FakeStation:
        def status(self):
            return {"on_air": True}
    spk = SpeakerOutput(command=["sh", "-c", "cat > /dev/null"])
    ctl = SpeakerControl(spk, lambda: None, lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/spectrum") as r:
            d = json.load(r)
        assert d["hz"][0] == 31.5 and len(d["db"]) == 28 and d["live"] is False
    finally:
        httpd.shutdown()
