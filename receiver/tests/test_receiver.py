"""The receiver service's own logic (no dongle, no network)."""
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import sleepradio_receiver as rx  # noqa: E402


def test_marine_and_two_metre_channels() -> None:
    marine = {c["name"].split(",")[0]: c for c in rx.marine_channels()}
    assert marine["Channel 16"]["freq"] == 156_800_000 and marine["Channel 16"]["priority"]
    assert marine["Channel 0"]["freq"] == 156_000_000 and marine["Channel 6"]["freq"] == 156_300_000
    assert marine["Channel 67"]["freq"] == 156_375_000 and marine["Channel 88"]["freq"] == 157_425_000
    assert "Channel 70" not in marine and len(marine) == 55                 # (70 is data, not speech)
    two = rx.DEFAULT_BANDS["2m"]["channels"]
    by = {c["freq"]: c for c in two}
    assert by[145_500_000]["priority"] and "Calling" in by[145_500_000]["name"] and "GB3HG" in by[145_625_000]["name"]
    assert 145_662_500 not in by and 145_800_000 in by and len(two) == 48    # (the D-STAR repeater is left out)


def test_what_to_listen_to() -> None:
    bands = rx.DEFAULT_BANDS
    assert rx.parse_spec({"band": ["2m"]}, bands) == {"band": "2m"}
    assert rx.parse_spec({"freq": ["145.5"]}, bands) == {"freq": 145_500_000, "mode": "nfm"}
    assert rx.parse_spec({"freq": ["156000000"], "squelch": ["off"]}, bands) == {"freq": 156_000_000, "mode": "nfm", "squelch": False}
    assert rx.parse_spec({"freq": "95.0", "mode": "WFM"}, bands) == {"freq": 95_000_000, "mode": "wfm"}
    for bad in ({"band": ["70cm"]}, {}, {"freq": ["abc"]}, {"freq": ["5"]}, {"freq": ["145.5"], "mode": ["ssb"]}):
        with pytest.raises(ValueError):
            rx.parse_spec(bad, bands)
    assert rx.spec_channels({"freq": 145_500_000, "mode": "nfm"}, bands) == ("145.5 MHz", [{"freq": 145_500_000, "name": "145.5 MHz"}])
    assert rx.spec_channels({"band": "marine"}, bands)[0] == "Marine VHF"


def test_the_dongles_centre_keeps_clear_of_the_channels() -> None:
    half = rx.SAMPLE_RATE_MS * 1e6 * rx.USABLE / 2
    for band in rx.DEFAULT_BANDS.values():
        if band["mode"] == "wfm" or "range" in band:                         # (one station, or one frequency, at a time: nothing to fit in)
            continue
        freqs = [c["freq"] for c in band["channels"]]
        centre = rx.centre_for(freqs)
        assert all(abs(f - centre) <= half for f in freqs)                   # everything is in what it hears well
        assert min(abs(f - centre) for f in freqs) >= 25_000                 # and nothing sits on the false carrier
    assert abs(rx.centre_for([145_500_000]) - 145_500_000) == 100_000
    with pytest.raises(ValueError):                                          # 2 m and marine together: two dongles' worth
        rx.centre_for([145_500_000, 156_000_000])
    conf = rx.airband_conf(rx.DEFAULT_BANDS["2m"]["channels"][:2], squelch=False)
    assert "fft_size = 512;" in conf and 'mode = "multichannel"' in conf and conf.count("udp_stream") == 2
    assert "dest_port = 47001" in conf and "squelch_threshold = -100" in conf and "freq = 145.212500" in conf
    assert "squelch_threshold" not in rx.airband_conf(rx.DEFAULT_BANDS["2m"]["channels"][:2])


def test_the_first_to_speak_has_the_air_and_a_priority_channel_takes_it() -> None:
    chans = [{"freq": 1, "name": "A"}, {"freq": 2, "name": "B"}, {"freq": 3, "name": "Calling", "priority": True}]
    m = rx.Mixer(chans, rate=16000, hang_s=2.0)
    pcm = bytes(4000)
    assert m.title(0.0, "2 metres") == "2 metres"
    assert m.packet(0, pcm, 0.1, 10.0) == pcm and m.title(10.0, "idle") == "A"
    assert m.packet(1, pcm, 0.1, 10.05) is None                  # B speaks over A: A keeps the air
    assert m.active(10.06) == [0, 1]                              # (but it's known to be active)
    assert m.packet(1, pcm, 0.1, 11.5) is None                    # A stopped 1.5 s ago: still held for the reply
    assert m.packet(1, pcm, 0.1, 12.1) == pcm and m.title(12.1, "idle") == "B"     # ... then B gets it
    assert m.packet(2, pcm, 0.1, 12.2) == pcm and m.title(12.2, "idle") == "Calling"   # priority: at once
    assert m.packet(1, pcm, 0.1, 12.3) is None                    # and B can't take it back
    assert m.title(20.0, "2 metres") == "2 metres"                # all quiet again


def test_a_carrier_that_stays_up_gives_way_and_can_be_skipped(tmp_path: Path) -> None:
    chans = [{"freq": 1, "name": "Gateway"}, {"freq": 2, "name": "B"}]
    m = rx.Mixer(chans, hang_s=2.0, hog_s=30.0)
    pcm, t = bytes(4000), 100.0
    while t < 125.0:                                              # the gateway's carrier, up all the while
        assert m.packet(0, pcm, 0.1, t) == pcm
        t += 0.125
    assert m.packet(1, pcm, 0.1, 125.0) is None                   # 25 s: still its air
    while t < 131.0:
        m.packet(0, pcm, 0.1, t)
        t += 0.125
    assert m.packet(1, pcm, 0.1, 131.0) == pcm and m.title(131.0, "") == "B"       # past 30 s: B, newly open, takes it
    assert m.packet(0, pcm, 0.1, 131.1) is None                   # and the carrier doesn't take it back
    assert m.packet(0, pcm, 0.1, 134.0) == pcm                    # until B has finished (2 s after its last)

    skips = tmp_path / "state" / "skip.json"
    r = rx.Receiver(rx.DEFAULT_BANDS, skips_file=skips)
    r.skip(145_237_500)
    assert json.loads(skips.read_text()) == [145_237_500] and r.status()["skipping"] == [145_237_500]
    name, left = rx.spec_channels({"band": "2m"}, r.bands, r.skips)
    assert len(left) == 47 and all(c["freq"] != 145_237_500 for c in left)
    assert rx.load_skips(skips) == {145_237_500}                  # remembered for the next start
    r.skip(145_237_500, on=False)
    assert json.loads(skips.read_text()) == [] and len(rx.spec_channels({"band": "2m"}, r.bands, r.skips)[1]) == 48
    assert "highpass = 300" in rx.airband_conf(chans) and "highpass" not in rx.airband_conf(chans, mode="am")


def test_silence_fills_the_gaps_at_real_time() -> None:
    m = rx.Mixer([{"freq": 1, "name": "A"}], rate=16000)
    assert m.filler(100.0) is None                                # (the clock starts here)
    out = [m.filler(100.0 + t / 100) for t in range(1, 101)]      # one second, asked every 10 ms
    assert sum(len(b) // 2 for b in out if b) == 16000            # exactly a second of silence
    assert m.packet(0, bytes(4000), 0.2, 101.01) is not None      # a transmission starts: its audio goes out
    assert m.filler(101.02) is None                               # and no silence on top of it
    total = 16000 + 2000 + sum(len(b) // 2 for t in range(0, 300) if (b := m.filler(101.02 + t / 100)))
    assert abs(total - 16000 * 4.02) <= 2000                      # still in step with the clock afterwards
    assert m.filler(500.0) is not None and m.sent == 2000         # after a long stall it starts afresh, no flood


def test_audio_framing() -> None:
    head = rx.wav_header(16000)
    assert head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt " and len(head) == 44
    assert struct.unpack("<HHI", head[20:28]) == (1, 1, 16000) and head[36:40] == b"data"
    block = rx.icy_block("Channel 16, Distress and calling")
    assert block[0] * 16 == len(block) - 1 and b"StreamTitle='Channel 16, Distress and calling';" in block
    assert "'it’s, fine';".encode() in rx.icy_block("it's; fine")   # (quotes and semicolons can't break the title)
    pcm, level = rx.to_pcm(struct.pack("<4f", 0.0, 0.5, -0.5, 2.0))
    assert struct.unpack("<4h", pcm) == (0, 16383, -16383, 32767) and 1.0 < level < 1.1


def test_extra_bands_from_a_file(tmp_path: Path) -> None:
    f = tmp_path / "bands.json"
    f.write_text('{"pmr": {"name": "PMR446", "channels": [{"freq": 446006250}, {"freq": 446018750, "name": "Two", "priority": true}]}}')
    bands = rx.load_bands(f)
    assert set(bands) == {*rx.DEFAULT_BANDS, "pmr"} and bands["pmr"]["channels"][0]["name"] == "446.0063"
    assert bands["pmr"]["channels"][1] == {"freq": 446_018_750, "name": "Two", "priority": True}
    assert set(rx.load_bands(tmp_path / "none.json")) == set(rx.DEFAULT_BANDS)


def test_a_band_of_your_own_from_a_stretch_of_spectrum(tmp_path: Path) -> None:
    pmr = rx.make_band({"name": " PMR  446 ", "from": 446.00625, "to": 446.09375, "step": 12.5, "priority": 446.00625})
    assert pmr["name"] == "PMR 446" and pmr["mode"] == "nfm" and len(pmr["channels"]) == 8
    assert pmr["channels"][0] == {"freq": 446_006_250, "name": "446.0063", "priority": True}
    assert pmr["channels"][-1] == {"freq": 446_093_750, "name": "446.0938"}
    air = rx.make_band({"name": "Airband", "mode": "am", "channels": [{"freq": "118.85", "name": "Teesside tower"}, {"freq": 118_850_000}, {"freq": 119.8}]})
    assert [c["name"] for c in air["channels"]] == ["Teesside tower", "119.8"] and air["mode"] == "am"     # (each frequency once)
    for bad, why in (({"from": 446, "to": 447}, "name"), ({"name": "x", "from": 145, "to": 156}, "one dongle"),
                     ({"name": "x", "from": 145, "to": 146, "step": 6.25}, "64 at most"), ({"name": "x", "from": 5, "to": 5.1}, "24 to 1766"),
                     ({"name": "x", "from": "abc", "to": 1}, "numbers"), ({"name": "x"}, "from, to"), ({"name": "x", "from": 145, "to": 145.1, "mode": "usb"}, "nfm or am"),
                     ({"name": "x", "from": 145, "to": 145.1, "step": 1}, "5 kHz"), ({"name": "x", "channels": "no"}, "from, to")):
        with pytest.raises(ValueError, match=why):
            rx.make_band(bad)

    kept = tmp_path / "state" / "bands.json"
    r = rx.Receiver(rx.DEFAULT_BANDS, bands_file=kept)
    key = r.set_band({"name": "PMR 446", "from": 446.00625, "to": 446.09375})
    assert key == "pmr-446" and r.status()["bands"]["pmr-446"] == {"name": "PMR 446", "channels": 8}
    assert rx.parse_spec({"band": ["pmr-446"]}, r.bands) == {"band": "pmr-446"}
    assert r.set_band({"name": "PMR 446", "from": 446.0, "to": 446.1, "step": 25}) == "pmr-446-2"      # a second of the same name
    assert r.set_band({"id": "pmr-446", "name": "PMR", "from": 446.00625, "to": 446.19375}) == "pmr-446" and len(r.bands["pmr-446"]["channels"]) == 16
    with pytest.raises(ValueError, match="of your own"):
        r.set_band({"id": "2m", "name": "Mine", "from": 145, "to": 145.1})
    with pytest.raises(ValueError, match="built in"):
        r.remove_band("marine")
    r.remove_band("pmr-446-2")
    again = rx.Receiver(rx.DEFAULT_BANDS, bands_file=kept)             # remembered for the next start
    assert set(again.bands) == {*rx.DEFAULT_BANDS, "pmr-446"} and again.bands["pmr-446"]["name"] == "PMR"
    assert "pmr-446" not in rx.DEFAULT_BANDS                              # (the built-in table isn't touched)
    kept.write_text('{"bad": {"channels": [{"freq": 1}]}, "ok": {"channels": [{"freq": 433500000}]}}')
    assert set(rx.Receiver(rx.DEFAULT_BANDS, bands_file=kept).own) == {"ok"}


def test_hold_one_channel_and_the_spectrum_for_a_waterfall() -> None:
    chans = [{"freq": 1, "name": "A"}, {"freq": 2, "name": "B"}, {"freq": 3, "name": "Calling", "priority": True}]
    m = rx.Mixer(chans, hang_s=2.0)
    pcm = bytes(4000)
    m.hold = 1
    assert m.title(5.0, "idle") == "B"                           # held: it's what you're on, open or not
    assert m.packet(0, pcm, 0.1, 10.0) is None and m.packet(2, pcm, 0.1, 10.1) is None    # not even the priority channel
    assert m.packet(1, pcm, 0.1, 10.2) == pcm and m.active(10.3) == [0, 1, 2]              # (the others are still seen)
    m.hold = None
    assert m.packet(2, pcm, 0.1, 10.4) == pcm and m.title(10.4, "idle") == "Calling"       # let go: the usual rules

    # eight bins of power in the FFT's order (bin 0 = the centre) -> low frequency first, a byte each
    power = [1.0, 10.0, 0.0, 0.0, 1e-4, 1e-9, 1e12, 0.1]
    row = rx.spectrum_row(struct.pack("<8f", *power))
    assert list(row) == [0, 0, 255, 75, 100, 125, 0, 0]          # -40 dB -> 0, 0 dB -> 100, 10 dB -> 125; nothing -> 0

    r = rx.Receiver(rx.DEFAULT_BANDS)
    with pytest.raises(ValueError, match="nothing is being received"):
        r.hold(145_500_000)
    assert r.waterfall() == {"running": False, "centre": 0, "rate": 2_400_000, "n": 0, "zero_db": -40, "per_db": 2.5, "rows": []}
    r.spec, r.name, r.mixer, r.centre = {"band": "2m"}, "2 metres", rx.Mixer(rx.DEFAULT_BANDS["2m"]["channels"]), 145_100_000

    class Running:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0
    r.proc = Running()
    r.hold(145_500_000)
    assert r.status()["hold"]["name"] == "145.5, Calling channel"
    with pytest.raises(ValueError, match="isn't a channel"):
        r.hold(145_501_000)
    r.hold(None)
    assert r.status()["hold"] is None
    for n in (1, 2, 3):
        r.spectrum.append((n, bytes([n] * 4)))
    r.spectrum_n = 3
    fall = r.waterfall(since=1)
    assert fall["running"] and fall["centre"] == 145_100_000 and fall["n"] == 3
    assert fall["rows"] == [[2, "AgICAg=="], [3, "AwMDAw=="]]
    r.proc = None


def test_a_channel_as_a_station_is_its_band_held_there(tmp_path: Path) -> None:
    bands = rx.DEFAULT_BANDS
    assert rx.parse_spec({"band": ["2m"], "hold": ["145.3"]}, bands) == {"band": "2m", "hold": 145_300_000}
    assert rx.parse_spec({"band": ["2m"], "hold": ["145300000"]}, bands) == {"band": "2m", "hold": 145_300_000}
    assert rx.parse_spec({"band": ["2m"], "hold": [""]}, bands) == {"band": "2m"}
    for bad, why in (({"band": ["2m"], "hold": ["156.8"]}, "isn't a channel of 2 metres"), ({"band": ["2m"], "hold": ["x"]}, "hold is a frequency")):
        with pytest.raises(ValueError, match=why):
            rx.parse_spec(bad, bands)

    class Running:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0
    r = rx.Receiver(bands, skips_file=tmp_path / "skip.json")
    asked = []
    def select(spec):                              # (no dongle here: note what would be received)
        asked.append(spec)
        r.spec, r.name, r.proc = spec, "2 metres", Running()
        if "band" in spec:
            r.mixer = rx.Mixer(rx.spec_channels(spec, r.bands, r.skips)[1])
    r.select = select
    r.listen({"band": "2m", "hold": 145_300_000})
    assert asked == [{"band": "2m"}] and r.status()["hold"]["freq"] == 145_300_000 and r.status()["receiving"] == {"band": "2m"}
    r.listen({"band": "2m", "hold": 145_500_000})                 # another of its channels: the band isn't started again
    assert r.status()["hold"]["name"] == "145.5, Calling channel"
    r.listen({"band": "2m"})                                      # the whole band: let go
    assert r.status()["hold"] is None and asked == [{"band": "2m"}] * 3
    r.skips.add(145_237_500)                                      # a skipped channel isn't in the band: on its own
    r.listen({"band": "2m", "hold": 145_237_500})
    assert asked[-1] == {"freq": 145_237_500, "mode": "nfm"}
    r.proc = None


def test_the_broadcast_band_is_one_station_at_a_time(tmp_path: Path) -> None:
    bands = rx.DEFAULT_BANDS
    fm = bands["fm"]
    assert fm["mode"] == "wfm" and len(fm["channels"]) == 206
    assert fm["channels"][0] == {"freq": 87_500_000, "name": "87.5 FM"} and fm["channels"][-1]["freq"] == 108_000_000
    assert rx.parse_spec({"band": ["fm"]}, bands) == {"band": "fm"}
    assert rx.parse_spec({"band": ["fm"], "hold": ["96.6"]}, bands) == {"band": "fm", "hold": 96_600_000}
    with pytest.raises(ValueError, match="isn't a channel of Broadcast FM"):
        rx.parse_spec({"band": ["fm"], "hold": ["145.5"]}, bands)
    assert rx.spec_channels({"freq": 96_600_000, "mode": "wfm", "of": "fm"}, bands)[0] == "96.6 FM"

    class Running:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0
    r = rx.Receiver(bands, skips_file=tmp_path / "skip.json")
    asked = []
    real = r.select
    def select(spec):                              # (no dongle here: note what would be received, as select() leaves things)
        if "band" in spec and r.bands[spec["band"]].get("mode") == "wfm":
            return real(spec)                      # (the real one hands a broadcast band on to its station)
        if spec == r.spec:
            return
        asked.append(spec)
        for lis in r.listeners:
            lis.dead = True
        r.listeners = []
        r.spec, r.name, r.proc, r.mixer, r.rate = spec, rx.spec_channels(spec, r.bands)[0], Running(), None, rx.WFM_RATE
    r.select = select
    a = r.listen({"band": "fm"})                                  # the band: somewhere to start
    assert asked == [{"freq": rx.FM_START, "mode": "wfm", "of": "fm"}] and a.rate == rx.WFM_RATE
    assert r.status()["receiving"] == {"freq": rx.FM_START, "mode": "wfm", "band": "fm"} and r.status()["name"] == "95.0 FM"
    seen = []                                                     # (never let go of, even for a moment: a stream closes on that)
    def select(spec, inner=select):
        seen.append(list(r.listeners))
        return inner(spec)
    r.select = select
    r.hold(96_600_000)                                            # another station: whoever is listening stays
    assert asked[-1] == {"freq": 96_600_000, "mode": "wfm", "of": "fm"} and r.listeners == [a] and not a.dead and seen[-1] == []
    b = r.listen({"band": "fm"})                                  # someone else tunes in to the band: where it is now
    assert len(asked) == 2 and r.listeners == [a, b]
    r.listen({"band": "fm", "hold": 89_100_000})                  # ...or to one of its stations: it moves, they stay
    assert asked[-1]["freq"] == 89_100_000 and len(r.listeners) == 3 and not a.dead and not b.dead
    r.select({"band": "fm"})                                      # (asked for as a band by /select too: not 206 channels at once)
    assert len(asked) == 3
    for bad, why in ((None, "one station at a time"), (145_500_000, "isn't a channel of Broadcast FM")):
        with pytest.raises(ValueError, match=why):
            r.hold(bad)
    r.listen({"freq": 95_000_000, "mode": "wfm"})                 # a frequency on its own is something else: they're let go
    assert asked[-1] == {"freq": 95_000_000, "mode": "wfm"} and a.dead and "band" not in r.status()["receiving"]
    r.proc = None


# --- sideband and Morse: one frequency, tuned like a rig (sleepradio_tuner.py) ---------------


def _demod(mode: str, offset: float, tones: list[tuple[float, float]], secs: float = 1.5):
    """What comes out for these tones (Hz from the dongle's centre, amplitude): (the strongest pitch, the audio's spectrum, its frequencies)."""
    import numpy as np
    import sleepradio_tuner as st
    d = st.Demod()
    d.set(offset, mode)
    step = st.HOP * st.HOPS
    n = int(secs * st.FS) // step * step
    t = np.arange(n) / st.FS
    x = sum(a * np.exp(2j * np.pi * f * t) for f, a in tones).astype(np.complex64)
    a = np.concatenate([d.process(x[i:i + step])[0] for i in range(0, n, step)]).astype(float)[st.AUDIO_RATE // 2:]
    S = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    f = np.fft.rfftfreq(len(a), 1 / st.AUDIO_RATE)
    return float(f[np.argmax(S)]), S, f


def test_sideband_and_morse_come_out_at_the_right_pitch():
    import numpy as np
    assert abs(_demod("usb", 30_000, [(31_000, 0.01)])[0] - 1000) < 3          # 1 kHz above the carrier: 1 kHz
    assert abs(_demod("lsb", -41_300, [(-41_900, 0.01)])[0] - 600) < 3         # 600 Hz below it, in lower sideband: 600 Hz
    assert abs(_demod("cw", 12_345, [(12_345, 0.01)])[0] - 700) < 3            # Morse on the frequency: the 700 Hz tone
    assert abs(_demod("cw", 12_345, [(12_495, 0.01)])[0] - 850) < 3
    # a station a hundred times stronger on the other sideband, and another 5 kHz off, make no difference
    pitch, S, f = _demod("usb", 30_000, [(31_000, 0.001), (29_000, 0.1), (36_000, 0.1)])
    assert abs(pitch - 1000) < 3 and np.max(S[np.abs(f - 1000) > 60]) < S[np.argmin(np.abs(f - 1000))] / 1000
    # ...and a steady tone comes out steady: nothing at the joins between the pieces it's made in
    pitch, S, f = _demod("usb", 30_000, [(31_234.5, 0.01)])
    assert np.max(S[np.abs(f - pitch) > 60]) < np.max(S) / 3000


def test_a_waterfall_row_is_the_band_lowest_frequency_first():
    import numpy as np
    import sleepradio_tuner as st
    d = st.Demod()
    step = st.HOP * st.HOPS
    t = np.arange(step) / st.FS
    row = st.spectrum_row(d.process((0.05 * np.exp(2j * np.pi * 64_000 * t)).astype(np.complex64))[1])
    assert len(row) == st.SPECTRUM_BINS and abs(int(np.argmax(np.frombuffer(row, np.uint8))) - 768) <= 1      # a quarter of the way down from the top
    assert st.centre_for(28_400_000) == 28_424_000


def test_tuned_bands_and_their_addresses():
    bands = rx.load_bands(None)
    assert bands["10m"]["range"] == [28_000_000, 29_700_000] and bands["10m"]["start"] == 28_400_000 and bands["10m"]["channels"] == []
    assert rx.parse_spec({"freq": ["28.5"], "mode": ["usb"]}, bands) == {"freq": 28_500_000, "mode": "usb"}
    assert rx.parse_spec({"freq": ["28.025"], "mode": ["cw"], "squelch": ["off"]}, bands) == {"freq": 28_025_000, "mode": "cw"}
    assert rx.parse_spec({"band": ["10m"], "hold": ["28.3"]}, bands) == {"band": "10m", "hold": 28_300_000}
    for bad in ({"band": ["10m"], "hold": ["27.9"]}, {"freq": ["28.5"], "mode": ["ssb"]}):
        try:
            rx.parse_spec(bad, bands)
            assert False, bad
        except ValueError:
            pass
    assert rx.spec_channels({"freq": 28_500_000, "mode": "usb", "of": "10m"}, bands)[0] == "28.5000 MHz USB"
    assert rx.spec_channels({"freq": 28_025_130, "mode": "cw"}, bands)[0] == "28.02513 MHz CW"


class _FakeTuner:
    """A Tuner with no dongle: it remembers what it was asked."""
    made: list = []

    def __init__(self, rtl_tcp, audio, row, **kw):
        self.audio, self.row, self.kw, self.asked, self.centre, self.level_db, self.stopped = audio, row, kw, [], 0, -60.0, False
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stderr=subprocess.PIPE)
        _FakeTuner.made.append(self)

    def start(self, freq, mode):
        self.tune(freq, mode, recentre=True)

    def tune(self, freq, mode, recentre=False):
        import sleepradio_tuner as st
        if recentre or abs(freq - self.centre) > st.USABLE_HZ:
            self.centre = st.centre_for(freq)
        self.asked.append((freq, mode))

    def stop(self):
        self.stopped = True


def test_a_tuned_band_is_moved_where_it_is_and_whoever_listens_stays(monkeypatch):
    import sleepradio_tuner as st
    _FakeTuner.made.clear()
    monkeypatch.setattr(st, "Tuner", _FakeTuner)
    r = rx.Receiver(rx.load_bands(None))
    try:
        lis = r.listen({"band": "10m"})
        t = _FakeTuner.made[0]
        assert lis.rate == st.AUDIO_RATE and t.asked == [(28_400_000, "usb")]
        s = r.status()
        assert s["receiving"] == {"freq": 28_400_000, "mode": "usb", "band": "10m"} and s["tunes"] and s["title"] == "28.4000 MHz USB"
        t.audio(b"\x01\x00" * 10)                        # its audio reaches the listener
        assert lis.chunks.get_nowait() == b"\x01\x00" * 10
        t.row(b"\x05" * 1024)                            # (nobody has asked for a waterfall: not kept)
        assert r.waterfall()["rows"] == [] and r.waterfall()["rate"] == st.FS and r.waterfall()["centre"] == 28_424_000
        t.row(b"\x05" * 1024)
        assert len(r.waterfall()["rows"]) == 1 and r.waterfall()["zero_db"] == st.ZERO_DB

        r.tune(28_450_000)                               # along the band: the same dongle, the same listener
        r.tune(mode="cw")
        assert t.asked[1:] == [(28_450_000, "usb"), (28_450_000, "cw")] and len(_FakeTuner.made) == 1 and not lis.dead
        assert r.status()["receiving"] == {"freq": 28_450_000, "mode": "cw", "band": "10m"} and r.listeners == [lis]
        r.tune(29_900_000, "usb")                        # off the end of the band: still tuned, the band let go of
        assert r.status()["receiving"] == {"freq": 29_900_000, "mode": "usb"} and r.waterfall()["centre"] == 29_924_000
        for bad in ((10_000_000, None), (None, "nfm")):
            try:
                r.tune(*bad)
                assert False, bad
            except ValueError:
                pass
        r.rest()
        assert t.stopped and r.tuner is None
        lis2 = r.listen({"band": "10m"})                 # the band again: where and how it was last
        assert _FakeTuner.made[1].asked == [(28_450_000, "cw")] and lis2.rate == st.AUDIO_RATE
        try:
            rx.Receiver(rx.load_bands(None)).tune(28_500_000)
            assert False
        except ValueError:
            pass
    finally:
        r.rest()
        for t in _FakeTuner.made:
            t.proc.kill()
