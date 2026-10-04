"""The receiver service's own logic (no dongle, no network)."""
import struct
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
    for bad in ({"band": ["70cm"]}, {}, {"freq": ["abc"]}, {"freq": ["5"]}, {"freq": ["145.5"], "mode": ["usb"]}):
        with pytest.raises(ValueError):
            rx.parse_spec(bad, bands)
    assert rx.spec_channels({"freq": 145_500_000, "mode": "nfm"}, bands) == ("145.5 MHz", [{"freq": 145_500_000, "name": "145.5 MHz"}])
    assert rx.spec_channels({"band": "marine"}, bands)[0] == "Marine VHF"


def test_the_dongles_centre_keeps_clear_of_the_channels() -> None:
    half = rx.SAMPLE_RATE_MS * 1e6 * rx.USABLE / 2
    for band in rx.DEFAULT_BANDS.values():
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
    assert set(bands) == {"2m", "marine", "pmr"} and bands["pmr"]["channels"][0]["name"] == "446.0063"
    assert bands["pmr"]["channels"][1] == {"freq": 446_018_750, "name": "Two", "priority": True}
    assert set(rx.load_bands(tmp_path / "none.json")) == {"2m", "marine"}
