"""Rooms: joining, who's talking, the voice frames, the register (no network, no decoder)."""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import pcm
from sleepradiopi.config.settings import load
from sleepradiopi.playback import radio, room, rooms_dir
from sleepradiopi.web.server import make_handler


def frame(talker: str, voice_bits=None, n: int = 0) -> bytes:
    """A reflector's frame: voice (five 49-bit frames, packed as a radio does) or, with none, a header."""
    body = np.zeros(720, dtype=np.uint8)
    if voice_bits is None:
        body[:] = np.random.default_rng(1).integers(0, 2, 720)          # (a header's bits: not voice)
    else:
        for j, bits in enumerate(voice_bits):
            block = np.concatenate([np.repeat(bits[:27], 3), bits[27:49], [0]]).astype(np.uint8) ^ room._WHITEN
            sent = np.zeros(104, dtype=np.uint8)
            sent[room._ORDER] = block
            body[j * 144 + 40:j * 144 + 144] = sent
    payload = bytes.fromhex("d471c9634d") + bytes(25) + np.packbits(body).tobytes()
    return b"YSFD" + b"GATEWAY   " + talker.ljust(10).encode() + b"ALL       " + bytes([n]) + payload


def speech(seed: int) -> list:
    bits = np.random.default_rng(seed).integers(0, 2, (5, 49)).astype(np.uint8)
    bits[:, 0:6] = 0                                                      # (a pitch that isn't the silence code)
    return list(bits)


SILENCE = np.array([1, 1, 1, 1, 1, 0] + [0] * 42 + [1], dtype=np.uint8)    # pitch field 125


class FakeSock:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    def sendto(self, m, where):
        self.sent.append((m, where))

    def recvfrom(self, n):
        if self.messages:
            return self.messages.pop(0), ("room", 1)
        time.sleep(0.01)
        raise socket.timeout

    def close(self):
        self.closed = True


class FakeDecoder:
    resets = 0

    def reset(self):
        FakeDecoder.resets += 1

    def decode(self, bits):
        return np.full(160, 4000, dtype=np.int16)


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_addresses_and_callsigns() -> None:
    assert room.parse("ysf://63.250.41.136:42000/US-America%20Link") == {"host": "63.250.41.136", "port": 42000, "name": "US-America Link"}
    assert room.parse("ysf://room.example") == {"host": "room.example", "port": 42000, "name": "room.example"}
    assert room.parse("http://room.example/") is None and not room.is_room("ysf://")
    assert room.link("1.2.3.4", 42200, "GB-CQ-UK ") == "ysf://1.2.3.4:42200/GB-CQ-UK"
    assert radio.validate_station({"name": "CQ-UK", "url": "ysf://1.2.3.4:42200/GB-CQ-UK"})["url"].startswith("ysf://")
    assert isinstance(radio.open_stream("ysf://1.2.3.4:42200/GB-CQ-UK"), room.RoomStream)
    assert room.clean_callsign(" m8odj ") == "M8ODJ" and room.clean_callsign("M0ABC-1") == "M0ABC-1" and room.clean_callsign("") == ""
    for bad in ("hello", "12345", "M0", "M0ABC!!", "M0ABCDEFGHIJK"):
        with pytest.raises(ValueError):
            room.clean_callsign(bad)


def test_voice_frames_are_unpacked_and_told_from_the_rest() -> None:
    want = speech(7)
    got, agree = room.voice(frame("M0ABC", want)[35:155])
    assert agree == 1.0 and all(np.array_equal(g, w) for g, w in zip(got, want))
    assert room.voice(frame("M0ABC")[35:155])[1] < 0.6                    # a header: the triples don't agree
    damaged = bytearray(frame("M0ABC", want))
    damaged[35 + 30 + 6] ^= 0x10                                          # one bit wrong on the air: outvoted
    got, agree = room.voice(bytes(damaged)[35:155])
    assert 0.99 <= agree < 1.0 and all(np.array_equal(g, w) for g, w in zip(got, want))
    assert room.is_silence(SILENCE) and not room.is_silence(want[0])


def test_a_room_joined_who_talks_and_what_is_heard() -> None:
    FakeDecoder.resets = 0
    status = b"YSFS" + b"00009" + b"CQ-UK".ljust(16) + b"CQ-UK Network ".ljust(14) + b"119"
    sock = FakeSock([b"YSFPREFLECTOR ", status, frame("M0ABC"), frame("M0ABC", speech(1), 2), frame("M0ABC", [SILENCE] * 5, 4),
                     frame("G4XYZ", speech(2), 0)])
    s = room.RoomStream("ysf://127.0.0.1:42200/GB-CQ-UK", callsign="m8odj", sock=lambda: sock, decoder=FakeDecoder)
    assert s.title == "GB-CQ-UK" and s.rig()["kind"] == "room"
    s.start()
    assert _wait(lambda: s.talker == "G4XYZ")
    assert sock.sent[0] == (b"YSFP" + b"M8ODJ     ", ("127.0.0.1", 42200)) and (b"YSFS", ("127.0.0.1", 42200)) in sock.sent
    assert s.title == "G4XYZ · GB-CQ-UK" and s.connected == 119 and s.has_decoder is True
    assert [h[0] for h in s.heard] == ["M0ABC"] and FakeDecoder.resets == 2          # a fresh start for each talker
    blocks = []
    assert _wait(lambda: (b := s.read(0.05)) is not None and blocks.append(b) is None and sum(len(x) for x in blocks) >= 8000)
    heard = np.concatenate(blocks)
    assert heard.shape[1] == pcm.CHANNELS and abs(int(heard.max()) - 4000) < 50      # the two spoken frames; the silent one isn't played
    assert _wait(lambda: s.talker is None, 4) and s.title == "GB-CQ-UK"               # the over ends when the frames stop
    state = s.rig()
    assert [h["call"] for h in state["heard"]] == ["G4XYZ", "M0ABC"] and state["connected"] == 119 and state["decoder"] is True
    with pytest.raises(ValueError, match="another room"):
        s.tune(freq=1)
    s.close()
    assert _wait(lambda: sock.closed) and sock.sent[-1][0] == b"YSFU" + b"M8ODJ     "    # it says it's leaving

    # with no decoder on the radio: who's talking is still shown; there's just no sound
    sock = FakeSock([frame("M0ABC", speech(1))])
    s = room.RoomStream("ysf://127.0.0.1:42200/GB-CQ-UK", callsign="M8ODJ", sock=lambda: sock, decoder=lambda: None)
    s.start()
    assert _wait(lambda: s.talker == "M0ABC") and s.has_decoder is False and s.rig()["decoder"] is False
    s.close()

    # and with no callsign it doesn't join at all
    sock = FakeSock([])
    s = room.RoomStream("ysf://127.0.0.1:42200/GB-CQ-UK", callsign="", sock=lambda: sock, decoder=lambda: None)
    s.start()
    assert _wait(lambda: s.ended is not None) and "callsign" in s.ended and sock.sent == []


def test_the_decoder_is_only_there_if_it_was_put_there(tmp_path: Path) -> None:
    assert room.load_decoder(tmp_path / "libmbe.so") is None
    (tmp_path / "libmbe.so").write_bytes(b"not a library")
    assert room.load_decoder(tmp_path / "libmbe.so") is None              # (and one that can't be loaded is passed over)


HOSTS = """# a comment
00009;GB-CQ-UK;Main Reflector;149.102.158.76;42200;000;
54321;US-America Link;America Link;63.250.41.136;42000;000;
00010;Twice;same address;63.250.41.136;42000;000;
bad line
00011;NoPort;x;host.example;;000;
""" + "".join(f"1{i:04d};Room {i};test;r{i}.example;42000;000;\n" for i in range(20))


def test_the_register_of_rooms(tmp_path: Path) -> None:
    found = rooms_dir.parse_hosts(HOSTS)
    assert len(found) == 22 and found[0] == {"id": "00009", "name": "GB-CQ-UK", "about": "Main Reflector", "url": "ysf://149.102.158.76:42200/GB-CQ-UK"}
    assert found[1]["url"] == "ysf://63.250.41.136:42000/US-America%20Link"
    asked = []
    book = rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=lambda u: asked.append(u) or HOSTS, clock=lambda: 100.0)
    assert [r["name"] for r in book.search("america link")] == ["US-America Link"] and asked == [rooms_dir.SOURCE]
    assert [r["name"] for r in book.search("00009")] == ["GB-CQ-UK"] and len(book.search("")) == 22
    again = rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=lambda u: 1 / 0, clock=lambda: 200.0)
    assert again.search("cq-uk")[0]["id"] == "00009" and again.status() == {"count": 22, "age_s": 100, "error": None}
    down = rooms_dir.RoomDirectory(tmp_path / "none.json", fetch=lambda u: 1 / 0)
    assert down.search("x") == [] and "couldn't be reached" in down.status()["error"]


def test_web_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rooms_dir, "_shared", rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=lambda u: HOSTS))
    monkeypatch.setattr(room, "CALLSIGN", None)
    monkeypatch.setattr(room, "DECODER", tmp_path / "libmbe.so")
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
        assert call("/api/rooms") == (200, {"callsign": "", "decoder": False, "decoder_path": str(tmp_path / "libmbe.so")})
        code, reply = call("/api/rooms/search?q=cq-uk")
        assert code == 200 and reply["results"][0]["url"] == "ysf://149.102.158.76:42200/GB-CQ-UK" and reply["directory"]["count"] == 22
        assert call("/api/rooms/callsign", {"callsign": "m8odj"}) == (200, {"callsign": "M8ODJ", "decoder": False})
        assert room.CALLSIGN == "M8ODJ" and load(conf).callsign == "M8ODJ" and call("/api/rooms")[1]["callsign"] == "M8ODJ"
        code, reply = call("/api/rooms/callsign", {"callsign": "not one"})
        assert code == 400 and "callsign" in reply["error"] and room.CALLSIGN == "M8ODJ"
        assert call("/api/rooms/callsign", {"callsign": ""})[1]["callsign"] == "" and room.CALLSIGN is None and load(conf).callsign is None
    finally:
        httpd.shutdown()
