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
    assert room.parse("ysf://63.250.41.136:42000/US-America%20Link") == {"net": "ysf", "host": "63.250.41.136", "port": 42000, "name": "US-America Link"}
    assert room.parse("ysf://room.example") == {"net": "ysf", "host": "room.example", "port": 42000, "name": "room.example"}
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
    fcs = "FCS00290;America-Link-WiresX;FCS002 - America-Link-WiresX;;;\n"
    both = lambda u: asked.append(u) or (fcs if u == rooms_dir.FCS_SOURCE else HOSTS)
    book = rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=both, clock=lambda: 100.0)
    assert [r["name"] for r in book.search("america link")] == ["America-Link-WiresX", "US-America Link"]
    assert asked == [rooms_dir.SOURCE, rooms_dir.FCS_SOURCE]
    assert [r["name"] for r in book.search("00009")] == ["GB-CQ-UK"] and len(book.search("")) == 23
    again = rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=lambda u: 1 / 0, clock=lambda: 200.0)      # kept: not asked for again
    assert again.search("cq-uk")[0]["id"] == "00009" and again.status() == {"count": 23, "age_s": 100, "error": None}
    # a list kept from before the FCS rooms were in it is fetched again in the background, though it's fresh
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"at": 250.0, "rooms": rooms_dir.parse_hosts(HOSTS)}))
    fresh = rooms_dir.RoomDirectory(old, fetch=lambda u: fcs if u == rooms_dir.FCS_SOURCE else HOSTS, clock=lambda: 300.0)
    assert len(fresh.rooms) == 22 and _wait(lambda: [r["id"] for r in fresh.search("wiresx")] == ["FCS00290"])
    down = rooms_dir.RoomDirectory(tmp_path / "none.json", fetch=lambda u: 1 / 0)
    assert down.search("x") == [] and "couldn't be reached" in down.status()["error"]


def test_web_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rooms_dir, "_shared", rooms_dir.RoomDirectory(tmp_path / "rooms.json", fetch=lambda u: "FCS00290;A;B;;;" if u == rooms_dir.FCS_SOURCE else HOSTS))
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
        assert call("/api/rooms") == (200, {"callsign": "", "decoder": False, "decoder_path": str(tmp_path / "libmbe.so"), "monitor": None})
        code, reply = call("/api/rooms/search?q=cq-uk")
        assert code == 200 and reply["results"][0]["url"] == "ysf://149.102.158.76:42200/GB-CQ-UK" and reply["directory"]["count"] == 23
        assert call("/api/rooms/callsign", {"callsign": "m8odj"}) == (200, {"callsign": "M8ODJ", "decoder": False})
        assert room.CALLSIGN == "M8ODJ" and load(conf).callsign == "M8ODJ" and call("/api/rooms")[1]["callsign"] == "M8ODJ"
        code, reply = call("/api/rooms/callsign", {"callsign": "not one"})
        assert code == 400 and "callsign" in reply["error"] and room.CALLSIGN == "M8ODJ"
        assert call("/api/rooms/callsign", {"callsign": ""})[1]["callsign"] == "" and room.CALLSIGN is None and load(conf).callsign is None
        assert call("/api/rooms/monitor", {"url": "ysf://a.example/A", "name": "A"})[0] == 400      # (no monitor on this one)
    finally:
        httpd.shutdown()


# --- monitor: a room over whatever's playing ---------------------------------------------------

from sleepradiopi.playback import monitor as mon          # noqa: E402


class FakeRoom:
    """A joined room as the monitor sees it: blocks to read, a talker, perhaps ended."""
    opened: list = []

    def __init__(self, url):
        self.url, self.blocks, self.talker, self.ended, self.closed, self.started = url, [], None, None, False, False
        FakeRoom.opened.append(self)

    def start(self):
        self.started = True

    def read(self, timeout=0.1):
        return self.blocks.pop(0) if self.blocks else None

    def close(self):
        self.closed = True

    def rig(self, since=0):
        return {"kind": "room", "base": self.url, "talker": self.talker}

    def say(self, seconds, level=6000):
        n = int(pcm.SAMPLE_RATE * seconds)
        for i in range(0, n, 4096):
            self.blocks.append(np.full((min(4096, n - i), pcm.CHANNELS), level, dtype=np.int16))


def test_a_monitored_room_comes_over_the_programme_and_goes_again() -> None:
    FakeRoom.opened = []
    now = [100.0]
    A, B = {"name": "CQ-UK", "url": "ysf://a.example:42000/A"}, {"name": "America", "url": "ysf://b.example:42000/B"}
    m = mon.Monitor([A, B], open_room=FakeRoom, clock=lambda: now[0])
    music = np.full((4410, pcm.CHANNELS), 10000, dtype=np.int16)              # a tenth of a second of programme

    def play(seconds):
        out = []
        for _ in range(round(seconds * 10)):
            out.append(m.mix(music.copy()))
            now[0] += 0.1
        return np.concatenate(out)

    out = play(0.5)
    assert np.array_equal(out, np.tile(music, (5, 1))) and [r.url for r in FakeRoom.opened] == [A["url"], B["url"]]     # joined; untouched
    a, b = FakeRoom.opened
    assert a.started and m.status()["talking"] is None and m.status()["rooms"][0]["joined"]
    a.blocks.append(np.zeros((4096, pcm.CHANNELS), dtype=np.int16))           # the room's own silence: nothing happens
    assert np.array_equal(play(0.2), np.tile(music, (2, 1)))

    a.talker = "M0ABC"
    a.say(2.0)
    b.say(1.0)                                                                # (someone in the other room too: the first has the air)
    out = play(3.0)[:, 0].astype(int)
    assert m.status()["talking"] == {"call": "M0ABC", "room": "CQ-UK"}
    assert out[-1] < 10000 * 0.3                                              # the programme is down
    assert out.max() > 10000 * 0.22 + 1000                                    # and the room is over it
    assert m.state(A["url"]) == {"kind": "room", "base": A["url"], "talker": "M0ABC", "monitor": True} and m.state("ysf://x/") is None
    a.talker = None
    out = play(3.0)[:, 0].astype(int)
    assert out[-1] == 10000 and m.status()["talking"] is None                 # back up, and exactly the programme again

    # the room that's the station itself just now isn't joined twice; afterwards it's joined again
    m.mix(music.copy(), playing_url=A["url"])
    assert a.closed and len(FakeRoom.opened) == 2
    m.mix(music.copy())
    assert len(FakeRoom.opened) == 3 and FakeRoom.opened[2].url == A["url"]
    # one that dropped is joined again, but not at once
    b.ended = "the room stopped answering"
    m.mix(music.copy())
    assert b.closed and len(FakeRoom.opened) == 3
    now[0] += mon.RETRY_S + 1
    m.mix(music.copy())
    assert len(FakeRoom.opened) == 4 and FakeRoom.opened[3].url == B["url"]
    m.set_rooms([B])
    assert FakeRoom.opened[2].closed and m.rooms == [B]

    for bad, why in (([{"name": "x", "url": "http://x/"}], "only a room"), ([{"name": str(i), "url": f"ysf://r{i}.example/"} for i in range(5)], "at most")):
        with pytest.raises(ValueError, match=why):
            mon.clean_rooms(bad)
    assert mon.clean_rooms([A, A]) == [A]
    assert mon.Monitor().mix(music) is music                                  # nothing monitored: the block itself


def test_monitoring_from_the_web(tmp_path: Path, monkeypatch) -> None:
    FakeRoom.opened = []
    monkeypatch.setattr(room, "CALLSIGN", "M8ODJ")

    class St:
        monitor = mon.Monitor(open_room=FakeRoom)
        radio_stream = None

    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(St, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    url = "ysf://a.example:42000/A"
    try:
        code, reply = call("/api/rooms/monitor", {"url": url, "name": "CQ-UK"})
        assert code == 200 and reply["monitor"]["rooms"] == [{"name": "CQ-UK", "url": url, "talker": None, "joined": False}]
        assert load(conf).monitor_rooms == [{"name": "CQ-UK", "url": url}] and call("/api/rooms")[1]["monitor"]["rooms"][0]["name"] == "CQ-UK"
        St.monitor.mix(np.zeros((100, pcm.CHANNELS), dtype=np.int16))        # the station plays something: the room is joined
        FakeRoom.opened[0].talker = "M0ABC"
        code, reply = call("/api/rx?base=" + urllib.request.quote(url, safe=""))
        assert code == 200 and reply == {"on": True, "kind": "room", "base": url, "talker": "M0ABC", "monitor": True}
        assert call("/api/rx") == (200, {"on": False})
        assert call("/api/rooms/monitor", {"url": "http://x/", "name": "x"})[0] == 400
        assert call("/api/rooms/callsign", {"callsign": "M0XYZ"})[0] == 200 and FakeRoom.opened[0].closed      # joined afresh under it
        code, reply = call("/api/rooms/monitor", {"url": url, "on": False})
        assert code == 200 and reply["monitor"]["rooms"] == [] and load(conf).monitor_rooms == []
    finally:
        httpd.shutdown()


# --- the frame's own header and data; rooms on the FCS network ---------------------------------

def _fold(bits100: np.ndarray) -> np.ndarray:
    """What room._unfold undoes: each bit as a pair, spread over 200."""
    out = np.zeros(200, dtype=np.uint8)
    d1 = d2 = d3 = d4 = 0
    for i, d in enumerate(int(b) for b in bits100):
        out[room._SPREAD[i]], out[room._SPREAD[i] + 1] = d ^ d3 ^ d4, d ^ d1 ^ d2 ^ d4
        d1, d2, d3, d4 = d, d1, d2, d3
    return out


def _crc(data: bytes) -> bytes:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return (crc ^ 0xFFFF).to_bytes(2, "big")


def radio_frame(fi: int, fn: int, dt: int = 2, voice_bits=None, data: bytes | None = None) -> bytes:
    """A whole 120-byte frame as a Fusion radio sends it: header, and for voice its five blocks and ten bytes of data."""
    four = bytes([fi << 6, fn << 3, dt, 0])
    six = np.unpackbits(np.frombuffer(four + _crc(four), dtype=np.uint8))
    words = np.concatenate([np.concatenate([six[k * 12:k * 12 + 12], np.zeros(12, dtype=np.uint8)]) for k in range(4)])
    head = np.packbits(_fold(np.concatenate([words, np.zeros(4, dtype=np.uint8)]))).tobytes()
    body = np.unpackbits(np.frombuffer(frame("X", voice_bits)[65:155], dtype=np.uint8)).copy()
    if data is not None:
        ten = bytes(b ^ w for b, w in zip(data.ljust(10)[:10], room._WHITEN20))
        coded = _fold(np.concatenate([np.unpackbits(np.frombuffer(ten + _crc(ten), dtype=np.uint8)), np.zeros(4, dtype=np.uint8)]))
        for j in range(5):
            body[j * 144:j * 144 + 40] = coded[j * 40:j * 40 + 40]
    return bytes.fromhex("d471c9634d") + head + np.packbits(body).tobytes()


def test_a_frames_header_and_the_callsign_riding_with_the_voice() -> None:
    f = radio_frame(1, 1, 2, speech(3), b"M0ABC")
    assert room.fich(f) == {"fi": 1, "fn": 1, "ft": 0, "dt": 2} and room.dch(f) == b"M0ABC     "
    assert room.fich(radio_frame(2, 0)) == {"fi": 2, "fn": 0, "ft": 0, "dt": 2}
    damaged = bytearray(f)
    damaged[7] ^= 0x40                                                    # a bit wrong in the header: not guessed at
    assert room.fich(bytes(damaged)) is None
    damaged = bytearray(f)
    damaged[31] ^= 0x01
    assert room.dch(bytes(damaged)) is None
    assert room.fich(frame("M0ABC", speech(1))[35:155]) is None           # (the plainer test frames have no header at all)


def test_a_room_on_the_fcs_network() -> None:
    assert room.parse("fcs://FCS00290/America-Link-WiresX") == {"net": "fcs", "host": "fcs002.xreflector.net", "port": 62500,
                                                                   "name": "America-Link-WiresX", "id": "FCS00290"}
    assert room.fcs_link("fcs00291", "CQ-UK-WiresX") == "fcs://FCS00291/CQ-UK-WiresX" and room.is_room("fcs://FCS00291/x")
    assert room.parse("fcs://FCS002/x") is None and room.parse("fcs://room.example/x") is None
    assert radio.validate_station({"name": "CQ-UK", "url": "fcs://FCS00291/CQ-UK-WiresX"})["url"] == "fcs://FCS00291/CQ-UK-WiresX"
    assert rooms_dir.parse_fcs("FCS00290;America-Link-WiresX;FCS002 - America-Link-WiresX;;;\nFCS00100;;empty;;;\nnonsense\n") == [
        {"id": "FCS00290", "name": "America-Link-WiresX", "about": "FCS002 - America-Link-WiresX", "url": "fcs://FCS00290/America-Link-WiresX"}]

    wrap = lambda f, n: f + bytes([n]) + b"FCS00290 "                     # the FCS envelope: 130 bytes, no callsigns outside the frame
    sock = FakeSock([b"ONLINE7", wrap(radio_frame(0, 0), 0), wrap(radio_frame(1, 0, 2, speech(1), b"ALL"), 2),
                     wrap(radio_frame(1, 1, 2, speech(2), b"G4XYZ"), 4), wrap(radio_frame(1, 2, 2, [SILENCE] * 5, b"x"), 6), wrap(radio_frame(2, 0), 8)])
    s = room.RoomStream("fcs://FCS00290/America-Link-WiresX", callsign="M8ODJ", sock=lambda: sock, decoder=FakeDecoder)
    import socket as _socket
    real = _socket.gethostbyname
    _socket.gethostbyname = lambda h: "10.0.0.1" if h == "fcs002.xreflector.net" else real(h)
    try:
        s.start()
        assert _wait(lambda: len(s.heard) == 1)
    finally:
        _socket.gethostbyname = real
    assert sock.sent[0] == (b"PING" + b"M8ODJ " + b"FCS00290" + bytes(7), ("10.0.0.1", 62500))
    info = next(m for m, _ in sock.sent if len(m) == 100)
    assert info.decode().split() == ["0", "0", "SleepRadio", "0"]            # once it had us: what we are
    assert list(s.heard)[0][0] == "G4XYZ" and s.talker is None and s.title == "America-Link-WiresX"      # named from the frames; over at the end frame
    blocks = []
    while (b := s.read(0.05)) is not None:
        blocks.append(b)
    assert blocks and int(np.concatenate(blocks).max()) > 3000              # the two spoken frames were played
    s.close()
    assert _wait(lambda: sock.closed) and sock.sent[-1][0] == b"CLOSE      "
