"""Internet receivers as stations (playback/receiver.py): OpenWebRX and KiwiSDR, with a stand-in for the WebSocket."""
import json
import struct
import time

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.playback import radio, receiver as rx


class FakeWs:
    """The receiver's end: gives its messages in order, then nothing; notes what it's sent."""

    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    def send(self, m):
        self.sent.append(m)

    def recv(self, timeout=None):
        if self.messages:
            m = self.messages.pop(0)
            return m(self) if callable(m) else m
        time.sleep(0.01)
        raise TimeoutError

    def close(self):
        self.closed = True

    def params(self):
        return [json.loads(m)["params"] for m in self.sent if m.startswith("{") and json.loads(m).get("type") == "dspcontrol"
                and "params" in json.loads(m)]


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _drain(s, wait=0.05):
    out = []
    while (b := s.read(wait)) is not None:
        out.append(b)
    return out


def _run(url, ws, text=lambda u: "{}"):
    def connect(u):                                # (a KiwiSDR's waterfall is a second socket: ws.wf, if the test has one)
        if u.endswith("/W/F"):
            if getattr(ws, "wf", None) is None:
                raise OSError("no waterfall")
            ws.wf.url = u
            return ws.wf
        ws.url = u
        return ws
    s = rx.ReceiverStream(url, connect=connect, get_text=text)
    s.start()
    return s


def test_which_addresses_are_receivers() -> None:
    assert rx.parse("http://sdr.example:8073/#freq=145500000,mod=nfm") == {
        "kind": "owrx", "base": "http://sdr.example:8073/", "freq": 145_500_000, "mode": "nfm"}
    assert rx.parse("https://vhf.example/#freq=145500000,mod=nfm,sql=-72")["sql"] == -72.0
    assert rx.parse("http://kiwi.example:8073/?f=7150.00lsbz10") == {
        "kind": "kiwi", "base": "http://kiwi.example:8073/", "freq": 7_150_000, "mode": "lsb"}
    assert rx.parse("http://kiwi.example:8073/?f=198")["mode"] == "am"
    for not_one in ("http://stream.radioparadise.com/aac-128", "http://x:8073/", "http://x/#freq=abc", "http://x/?f=abc",
                    "http://192.168.50.4:8074/audio?freq=145.5", "ftp://x/#freq=1,mod=nfm", "http://x/#freq=145500000,mod=xyz"):
        assert rx.parse(not_one) is None and not rx.is_receiver(not_one)
    assert rx.said({"freq": 145_500_000, "mode": "nfm"}) == "145.5 MHz FM" and rx.said({"freq": 7_150_000, "mode": "lsb"}) == "7150 kHz LSB"
    assert isinstance(radio.open_stream("http://kiwi.example:8073/?f=198am"), rx.ReceiverStream)
    assert isinstance(radio.open_stream("http://stream.radioparadise.com/aac-128"), radio.RadioStream)


def test_audio_decoding_and_resampling() -> None:
    d = rx.Adpcm()
    out = d.decode(b"junk" + b"SYNC" + struct.pack("<hh", 20, 1000) + bytes([0x00] * 10))
    assert len(out) == 20 and d.phase == 2 and abs(int(out[0]) - 1000) < 50     # (starts from the state it was sent)
    d2 = rx.Adpcm()                                # a sync word split across two packets is still found
    assert len(d2.decode(b"SY")) == 0 and len(d2.decode(b"NC" + struct.pack("<hh", 0, 0) + b"\x07\x07")) == 4
    r = rx.Resampler(12000)
    x = (np.sin(np.arange(24000) * 2 * np.pi * 440 / 12000) * 10000).astype(np.int16)
    y = np.concatenate([r.process(x[i:i + 517]) for i in range(0, len(x), 517)])
    assert y.shape == (88200, pcm.CHANNELS) and abs(int(np.abs(y).max()) - 10000) < 300
    assert np.abs(np.diff(y[:, 0].astype(int))).max() < 700                   # no clicks where the packets join
    assert len(r.process(np.zeros(0, dtype=np.int16))) == 0


def test_openwebrx_tunes_sets_the_squelch_and_plays() -> None:
    config = json.dumps({"type": "config", "value": {"center_freq": 145_000_000, "samp_rate": 2_048_000, "squelch_auto_margin": 10,
                                                      "audio_compression": "none"}})
    smeter = lambda v: json.dumps({"type": "smeter", "value": v})
    audio = b"\x02" + (np.ones(1200, dtype="<i2") * 5000).tobytes()
    def later(m, s=1.3):
        def f(ws):
            time.sleep(s)
            return m
        return f
    ws = FakeWs(["CLIENT DE SERVER server=openwebrx version=v1.2.110", config, audio, smeter(1e-7), smeter(1e-7), smeter(1e-3),
                 later(smeter(1e-7)), later(smeter(1e-7)), audio, audio])
    old, rx.SQUELCH_LEARN_S = rx.SQUELCH_LEARN_S, 2.0
    try:
        s = _run("http://sdr.example:8073/#freq=145500000,mod=nfm", ws,
                 text=lambda u: json.dumps({"receiver": {"name": "M0ABC"}}))
        assert _wait(lambda: len(ws.params()) >= 2, 6)
    finally:
        rx.SQUELCH_LEARN_S = old
    assert ws.url == "ws://sdr.example:8073/ws/" and ws.sent[0].startswith("SERVER DE CLIENT")
    tune, squelch = ws.params()[0], ws.params()[1]
    assert tune["offset_freq"] == 500_000 and tune["mod"] == "nfm" and tune["squelch_level"] == -150
    assert -61 <= squelch["squelch_level"] <= -59          # the quiet readings (-70 dB) + the receiver's margin; not the burst
    assert _wait(lambda: any(np.abs(b).max() > 0 for b in _drain(s)), 3)   # then its audio is played
    assert s.title == "145.5 MHz FM · M0ABC" and s.ended is None
    s.close()
    assert _wait(lambda: ws.closed)


def test_openwebrx_asks_for_the_band_that_has_the_frequency_or_says_why_not() -> None:
    status = json.dumps({"sdrs": [{"name": "RTL-SDR", "profiles": [{"name": "2m", "center_freq": 145_000_000, "sample_rate": 2_048_000},
                                                                    {"name": "Marine", "center_freq": 156_000_000, "sample_rate": 2_048_000}]}]})
    config = lambda c: json.dumps({"type": "config", "value": {"center_freq": c, "samp_rate": 2_048_000}})
    profiles = json.dumps({"type": "profiles", "value": [{"id": "rtlsdr|2m", "name": "RTL-SDR 2m"}, {"id": "rtlsdr|m", "name": "RTL-SDR Marine"}]})
    ws = FakeWs([config(145_000_000), profiles, config(156_000_000)])
    s = _run("http://sdr.example:8073/#freq=156800000,mod=nfm,sql=-60", ws, text=lambda u: status)
    assert _wait(lambda: ws.params())
    assert {"type": "selectprofile", "params": {"profile": "rtlsdr|m"}} in [json.loads(m) for m in ws.sent if m.startswith("{")]
    assert ws.params()[0]["offset_freq"] == 800_000 and ws.params()[0]["squelch_level"] == -60     # (the link's own squelch)
    s.close()
    ws = FakeWs([config(145_000_000), profiles])            # ...and a frequency none of its bands has
    s = _run("http://sdr.example:8073/#freq=433500000,mod=nfm", ws, text=lambda u: status)
    assert _wait(lambda: s.ended is not None) and "no band with 433.5 MHz FM" in s.ended
    ws = FakeWs([json.dumps({"type": "backoff", "value": "too many"})])
    s = _run("http://sdr.example:8073/#freq=145500000,mod=nfm", ws)
    assert _wait(lambda: s.ended == "the receiver is full")


def test_kiwisdr_tunes_and_plays_and_says_when_its_full() -> None:
    snd = b"SND" + bytes([0]) + struct.pack("<I", 1) + struct.pack(">H", 400) + (np.ones(600, dtype=">i2") * 3000).tobytes()
    ws = FakeWs([b"MSG sample_rate=11998.858216", b"MSG badp=0", snd, snd])
    s = _run("http://kiwi.example:8073/?f=7150.00lsb", ws, text=lambda u: "name=G0XYZ, Somewhere\nusers=1\n")
    assert _wait(lambda: any("SET mod=" in m for m in ws.sent))
    assert ws.url.startswith("ws://kiwi.example:8073/kiwi/") and ws.url.endswith("/SND") and ws.sent[0] == "SET auth t=kiwi p="
    assert "SET mod=lsb low_cut=-2700 high_cut=-300 freq=7150.000" in ws.sent and "SET compression=0" in ws.sent
    assert _wait(lambda: any(np.abs(b).max() > 2000 for b in _drain(s)), 3)
    assert s.title == "7150 kHz LSB · G0XYZ, Somewhere"
    s.close()
    ws = FakeWs([b"MSG too_busy=8"])
    s = _run("http://kiwi.example:8073/?f=198am", ws)
    assert _wait(lambda: s.ended is not None) and "full" in s.ended
    s = rx.ReceiverStream("http://kiwi.example:8073/?f=198am", connect=lambda u: (_ for _ in ()).throw(OSError("no route")), get_text=lambda u: "")
    s.start()
    assert _wait(lambda: s.ended is not None) and "couldn't be reached" in s.ended


def test_silence_goes_out_in_real_time_when_the_receiver_is_quiet() -> None:
    now = [100.0]
    s = rx.ReceiverStream("http://sdr.example:8073/#freq=145500000,mod=nfm", clock=lambda: now[0])
    assert s.fill(100.0) == 0                              # (the clock starts)
    for step in range(1, 201):                             # two seconds, looked at every 10 ms
        now[0] = 100.0 + step / 100
        s.fill(now[0])
    assert abs(s._sent - 2 * pcm.SAMPLE_RATE) <= pcm.CHUNK_FRAMES // 4 + 500
    s._audio(np.ones((4410, pcm.CHANNELS), dtype=np.int16))      # a transmission: its audio, and no silence on top of it
    assert s.fill(now[0] + 0.1) == 0
    now[0] += 60.0                                         # after a long stall it starts afresh rather than flooding
    assert s.fill(now[0]) == pcm.CHUNK_FRAMES
    blocks = _drain(s, 0.01)
    assert blocks and all(b.shape == (pcm.CHUNK_FRAMES, pcm.CHANNELS) for b in blocks)


def test_kiwisdr_for_the_rig_its_waterfall_its_meter_and_tuning_where_it_is() -> None:
    snd = b"SND" + bytes([0]) + struct.pack("<I", 1) + struct.pack(">H", 270) + (np.ones(600, dtype=">i2") * 3000).tobytes()
    row = lambda x_bin, zoom, fill: b"W/F" + bytes([0]) + struct.pack("<III", x_bin, zoom, 0) + bytes([fill]) * 1024
    ws = FakeWs([b"MSG sample_rate=12000", snd])
    # zoom 6 of 14 round 7100 kHz: 30 MHz / 64 = 468.75 kHz across, from 6865.625 kHz
    ws.wf = FakeWs([b"MSG wf_setup zoom_max=14 bandwidth=30000000", row(3839658, 6, 155), row(3839658, 6, 160)])
    s = rx.ReceiverStream("http://kiwi.example:8073/?f=7150.00lsb", get_text=lambda u: "name=G0XYZ\n",
                          connect=lambda u: (ws.wf if u.endswith("/W/F") else ws).__setattr__("url", u) or (ws.wf if u.endswith("/W/F") else ws))
    s.rig()                                        # (someone is looking: rows are kept)
    s.start()
    assert _wait(lambda: s.rig()["n"] == 2 and s.rig()["dbm"] is not None)
    assert ws.wf.url.endswith("/W/F") and ws.url.rsplit("/", 2)[1] == ws.wf.url.rsplit("/", 3)[1]      # the same stamp pairs them
    assert "SET zoom=6 cf=7150.000" in ws.wf.sent and "SET wf_comp=0" in ws.wf.sent
    state = s.rig()
    assert (state["kind"], state["freq"], state["mode"], state["name"], state["tunes"]) == ("kiwi", 7_150_000, "lsb", "G0XYZ", True)
    assert state["dbm"] == -100.0 and abs(state["lo"] - 6_865_844) < 2 and state["hi"] - state["lo"] == 468_750
    assert [n for n, _ in state["rows"]] == [1, 2] and len(rx.base64.b64decode(state["rows"][0][1])) == 1024
    assert [n for n, _ in s.rig(since=1)["rows"]] == [2]

    s.tune(freq=14_200_000, mode="usb", zoom=7, centre=14_175_000)             # another band, on the same connection
    assert ws.sent[-1] == "SET mod=usb low_cut=300 high_cut=2700 freq=14200.000" and ws.wf.sent[-1] == "SET zoom=7 cf=14175.000"
    assert s.title == "14200 kHz USB · G0XYZ" and s.rig()["freq"] == 14_200_000
    ws.wf.messages.append(row(7927398, 7, 150))    # the new view's first row: the old view's rows are no longer given
    assert _wait(lambda: s.rig()["n"] == 3)
    assert [n for n, _ in s.rig()["rows"]] == [3] and s.rig()["hi"] - s.rig()["lo"] == 234_375
    s.tune(freq=14_205_000)                        # (the mode stays)
    assert ws.sent[-1] == "SET mod=usb low_cut=300 high_cut=2700 freq=14205.000"
    s.tune(freq=14_030_250, mode="cw")             # Morse: the frequency is the signal's, the receiver set below it
    assert ws.sent[-1] == "SET mod=cw low_cut=300 high_cut=700 freq=14029.750" and s.rig()["freq"] == 14_030_250
    assert (rx.dial("kiwi", 7_030_000, "cw"), rx.dial("owrx", 7_030_000, "cw"), rx.dial("kiwi", 7_150_000, "lsb")) == (7_029_500, 7_029_200, 7_150_000)
    s.tune(freq=14_205_000, mode="usb")
    for bad, why in (({"mode": "wfm"}, "modes"), ({"freq": 99_000_000}, "30 MHz")):
        try:
            s.tune(**bad)
            raise AssertionError("took it")
        except ValueError as e:
            assert why in str(e)
    s.close()
    assert ws.closed and ws.wf.closed



def test_openwebrx_for_the_rig_its_waterfall_its_bands_and_tuning_where_it_is() -> None:
    conf = lambda **v: json.dumps({"type": "config", "value": v})
    fft = b"\x01" + bytes(range(64))
    ws = FakeWs([conf(center_freq=145_000_000, samp_rate=2_000_000, fft_compression="adpcm", sdr_id="rtl", profile_id="2m", squelch_auto_margin=10),
                 json.dumps({"type": "profiles", "value": [{"id": "rtl|2m", "name": "2m"}, {"id": "rtl|40m", "name": "40m"}]}), fft])
    s = rx.ReceiverStream("http://owrx.example/#freq=145500000,mod=nfm", connect=lambda u: ws, get_text=lambda u: json.dumps({"receiver": {"name": "M0XYZ"}}))
    s.rig()
    s.start()
    assert _wait(lambda: s.rig()["n"] == 1 and ws.params())
    state = s.rig()
    assert (state["kind"], state["tunes"], state["codec"], state["band"], state["name"]) == ("owrx", True, "adpcm", "rtl|2m", "M0XYZ")
    assert (state["lo"], state["hi"]) == (144_000_000, 146_000_000) and state["bands"][1] == {"id": "rtl|40m", "name": "40m"}
    assert rx.base64.b64decode(state["rows"][0][1]) == bytes(range(64))                     # as it was sent: the page works it out

    s.tune(freq=145_550_000)                                                               # in the band: on the same connection
    assert _wait(lambda: ws.params()[-1]["offset_freq"] == 550_000) and s.rig()["freq"] == 145_550_000
    s.tune(mode="am")
    assert _wait(lambda: ws.params()[-1]["mod"] == "am" and ws.params()[-1]["offset_freq"] == 550_000)
    assert s.title == "145.55 MHz AM · M0XYZ"
    for bad, why in (({"freq": 7_100_000}, "outside the band"), ({"profile": "rtl|70cm"}, "no such band"), ({"mode": "nbfm"}, "modes")):
        try:
            s.tune(**bad)
            raise AssertionError("took it")
        except ValueError as e:
            assert why in str(e)

    s.tune(profile="rtl|40m")                                                              # another band: it says where that starts
    assert _wait(lambda: any('"selectprofile"' in m and "rtl|40m" in m for m in ws.sent))
    ws.messages.append(conf(center_freq=7_100_000, samp_rate=500_000, start_freq=7_150_000, start_mod="lsb", profile_id="40m"))
    assert _wait(lambda: ws.params()[-1]["mod"] == "lsb" and ws.params()[-1]["offset_freq"] == 50_000)
    assert s.rig()["freq"] == 7_150_000 and s.rig()["band"] == "rtl|40m" and s.rig()["rows"] == []      # the old band's waterfall is gone
    time.sleep(0.2)                                # (six frames a second are kept, no more)
    ws.messages.append(fft)
    assert _wait(lambda: len(s.rig()["rows"]) == 1) and s.rig()["lo"] == 6_850_000
    s.close()
