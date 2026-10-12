"""Reporting what's heard to PSK Reporter and WSPRnet (audio/report.py)."""
import json
import struct
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import ft8, pcm, report
from sleepradiopi.playback import room
from sleepradiopi.web.server import make_handler

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _reporter(who=("M0ABC", "IO94kn")):
    clock, sent, posted = Clock(T0), [], []
    r = report.Reporter(lambda: who, clock=clock, send=sent.append, post=posted.append)
    return r, clock, sent, posted


def _on(r: report.Reporter) -> None:
    """Switched on without its thread: stepped here."""
    r.switch(True)
    r._stop.set()


def _strings(body: bytes, n: int) -> tuple[list[str], bytes]:
    out = []
    for _ in range(n):
        out.append(body[1:1 + body[0]].decode())
        body = body[1 + body[0]:]
    return out, body


def test_the_descriptors_are_as_psk_reporter_gives_them() -> None:
    # (the receiver's: an options template of three of its own fields, each a string; padded to fours)
    assert report.RX_DESCRIPTOR == bytes.fromhex("0003 0024 50E2 0003 0000  8002 FFFF 0000768F  8004 FFFF 0000768F  8008 FFFF 0000768F  0000")
    # (a station heard: callsign, frequency, sNR, mode, locator, informationSource, flowStartSeconds)
    assert report.TX_DESCRIPTOR == bytes.fromhex("0002 003C 50E3 0007  8001 FFFF 0000768F  8005 0004 0000768F  8006 0001 0000768F  800A FFFF 0000768F"
                                                 "  8003 FFFF 0000768F  800B 0001 0000768F  0096 0004")
    assert len(report.RX_DESCRIPTOR) == 36 and len(report.TX_DESCRIPTOR) == 60


def test_a_datagram_says_who_heard_whom() -> None:
    spots = [{"call": "K1ABC", "hz": 14_075_100, "snr": -12.4, "mode": "ft8", "grid": "FN42", "t": T0},
             {"call": "PJ4/DL1ABC", "hz": 14_081_250, "snr": 3.0, "mode": "ft4", "grid": "", "t": T0 + 15}]
    [packet] = report.datagrams("M0ABC", "IO94kn", "Sleep Radio 1.9.0", spots, T0 + 300, 7, 0xDEADBEEF, True)
    version, length, when, sequence, ident = struct.unpack("!HHIII", packet[:16])
    assert (version, length, when, sequence, ident) == (10, len(packet), int(T0 + 300), 7, 0xDEADBEEF) and len(packet) % 4 == 0
    rest = packet[16:]
    assert rest.startswith(report.RX_DESCRIPTOR + report.TX_DESCRIPTOR)
    rest = rest[96:]
    link, n = struct.unpack("!HH", rest[:4])
    assert link == report.RX_LINK and n % 4 == 0
    assert _strings(rest[4:n], 3)[0] == ["M0ABC", "IO94kn", "Sleep Radio 1.9.0"]
    rest = rest[n:]
    link, n = struct.unpack("!HH", rest[:4])
    assert link == report.TX_LINK and n == len(rest) and n % 4 == 0
    body = rest[4:]
    for spot, mode in zip(spots, ("FT8", "FT4")):
        [call], body = _strings(body, 1)
        hz, snr = struct.unpack("!Ib", body[:5])
        [said, grid], body = _strings(body[5:], 2)
        source, t = struct.unpack("!BI", body[:5])
        body = body[5:]
        assert (call, hz, snr, said, grid, source, t) == (spot["call"], spot["hz"], round(spot["snr"]), mode, spot["grid"], 1, int(spot["t"]))
    assert not body.strip(b"\0")
    # without the descriptors, and too many for one datagram: each under the size, the sequence counting reports
    many = [{"call": f"G{i:03d}ABC", "hz": 7_074_000 + i, "snr": -5, "mode": "ft8", "grid": "IO91", "t": T0} for i in range(120)]
    packets = report.datagrams("M0ABC", "IO94", "x", many, T0, 100, 1, False)
    assert len(packets) > 1 and all(len(p) <= report.DATAGRAM_MOST and len(p) % 4 == 0 and report.TX_DESCRIPTOR not in p for p in packets)
    counts = []
    for p in packets:
        rx = struct.unpack("!HH", p[16:20])[1]
        counts.append(p[16 + rx + 4:].count(b"\x03FT8"))
    assert sum(counts) == 120 and [struct.unpack("!I", p[8:12])[0] for p in packets] == [100 + sum(counts[:i]) for i in range(len(packets))]


def test_off_until_switched_on_and_not_without_a_callsign_and_locator() -> None:
    r, clock, sent, posted = _reporter()
    r.heard("ft8", 14_074_000, T0, [{"call": "K1ABC", "hz": 1100, "snr": -3.0}])
    clock.at += 1000
    r.step()
    assert sent == [] and r.status()["on"] is False and r.status()["waiting"] == 0       # off: nothing kept, nothing sent
    for who in ((None, "IO94"), ("M0ABC", None), (None, None)):
        nobody = report.Reporter(lambda: who)
        with pytest.raises(ValueError, match="callsign"):
            nobody.switch(True)
        assert nobody.on is False and nobody.status()["can"] is False


def test_the_pace_psk_reporter_asks_for() -> None:
    r, clock, sent, posted = _reporter()
    _on(r)
    r.heard("ft8", 14_074_000, T0, [{"call": "K1ABC", "hz": 1100, "snr": -3.0, "grid": "FN42"}, {"snr": 0.0, "hz": 900, "text": "TNX 73"}])
    r.heard("ft8", 14_074_000, T0 + 15, [{"call": "K1ABC", "hz": 1100, "snr": 2.0}])        # again, with no locator: the one it gave is kept
    clock.at = T0 + report.SEND_EVERY_S - 1
    r.step()
    assert sent == [] and r.status()["waiting"] == 1          # not before five minutes are up
    clock.at = T0 + report.SEND_EVERY_S + report.JITTER_S
    r.step()
    assert len(sent) == 1 and report.TX_DESCRIPTOR in sent[0] and b"\x05K1ABC" in sent[0] and b"\x04FN42" in sent[0] and sent[0].count(b"K1ABC") == 1
    st = r.status()
    assert st["sent"] == {"psk": 1, "wspr": 0} and st["last"]["n"] == 1 and st["waiting"] == 0 and 0 < st["next_s"] <= report.SEND_EVERY_S + report.JITTER_S
    r.heard("ft8", 14_074_000, clock.at, [{"call": "K1ABC", "hz": 1100, "snr": -3.0}, {"call": "G4XYZ", "hz": 700, "snr": -9.0, "grid": "IO91"}])
    r.heard("ft8", 7_074_000, clock.at, [{"call": "K1ABC", "hz": 1500, "snr": -15.0}])     # another band: reported there too
    clock.at += report.SEND_EVERY_S + report.JITTER_S
    r.step()
    assert len(sent) == 2 and b"G4XYZ" in sent[1] and sent[1].count(b"K1ABC") == 1          # the same station on the same band: not again so soon
    assert struct.unpack("!I", sent[1][8:12])[0] == 1 and sent[0][12:16] == sent[1][12:16]  # the sequence counts reports; the same identifier
    clock.at += report.SEND_EVERY_S + report.JITTER_S
    r.step()
    assert len(sent) == 2                                     # nothing new: nothing sent
    for i in range(2):                                        # the descriptors: with the first three datagrams, then left out
        r.heard("ft8", 14_074_000, clock.at, [{"call": f"DL{i}ABC", "hz": 800, "snr": 1.0}])
        clock.at += report.SEND_EVERY_S + report.JITTER_S
        r.step()
    assert len(sent) == 4 and report.TX_DESCRIPTOR in sent[2] and report.TX_DESCRIPTOR not in sent[3]
    clock.at += report.AGAIN_AFTER_S                          # half an hour on: K1ABC is news again; and the descriptors go once an hour
    r.heard("ft8", 14_074_000, clock.at, [{"call": "K1ABC", "hz": 1100, "snr": -3.0}])
    clock.at += report.SEND_EVERY_S + report.JITTER_S + report.DESCRIPTORS_EVERY_S
    r.step()
    assert len(sent) == 5 and b"K1ABC" in sent[4] and report.TX_DESCRIPTOR in sent[4]
    r.switch(False)                                           # off: what was waiting is dropped
    r.heard("ft8", 14_074_000, clock.at, [{"call": "F5ABC", "hz": 800, "snr": 1.0}])
    assert r.status()["waiting"] == 0


def test_a_send_that_fails_is_said_and_tried_no_harder() -> None:
    def fail(packet: bytes) -> None:
        raise OSError("Network is unreachable")
    clock = Clock(T0)
    r = report.Reporter(lambda: ("M0ABC", "IO94"), clock=clock, send=fail)
    _on(r)
    r.heard("ft8", 14_074_000, T0, [{"call": "K1ABC", "hz": 1100, "snr": -3.0}])
    clock.at += report.SEND_EVERY_S + report.JITTER_S
    r.step()
    st = r.status()
    assert st["sent"]["psk"] == 0 and "unreachable" in st["last"]["error"] and st["waiting"] == 0


def test_wspr_goes_to_wsprnet_a_spot_at_a_time() -> None:
    r, clock, sent, posted = _reporter()
    _on(r)
    t = 1_800_000_080.0 // 120 * 120
    r.heard("wspr", 14_095_600, t, [{"call": "K1ABC", "grid": "FN42", "hz": 1512, "snr": -21.0, "dt": 0.3, "drift": -1, "dbm": 37},
                                    {"call": "PJ4/K1ABC", "hz": 1480, "snr": -25.0, "dt": 0.1, "drift": 0, "dbm": 30}])        # (no locator: WSPRnet wants one)
    r.step()
    assert sent == [] and len(posted) == 1
    q = posted[0]
    assert (q["function"], q["rcall"], q["rgrid"], q["rqrg"], q["tqrg"], q["tcall"], q["tgrid"], q["sig"], q["dt"], q["drift"], q["dbm"], q["mode"]) == \
        ("wspr", "M0ABC", "IO94kn", "14.095600", "14.097112", "K1ABC", "FN42", "-21", "0.3", "-1", "37", "2")
    import time as time_mod
    assert q["date"] == time_mod.strftime("%y%m%d", time_mod.gmtime(t)) and q["time"] == time_mod.strftime("%H%M", time_mod.gmtime(t))
    assert r.status()["sent"] == {"psk": 0, "wspr": 1}

    def fail(query: dict) -> None:
        raise urllib.error.URLError("no route")
    r._post = fail
    r.heard("wspr", 14_095_600, t + 120, [{"call": "G4XYZ", "grid": "IO91", "hz": 1500, "snr": -11.0, "dt": 0.0, "drift": 0, "dbm": 23}])
    r.step()
    assert "no route" in r.status()["last"]["error"] and r.status()["sent"]["wspr"] == 1


def _reader(found, own, dial, playing):
    clock, told = Clock(T0 - 1.0), []
    r = ft8.Reader(clock=clock, decode=lambda samples, mode: [dict(m) for m in found])
    r.on, r.band, r.dial, r.own = True, "20 m", dial, own
    r.playing = lambda: playing
    r.report = lambda mode, dial, t0, found: told.append((mode, dial, t0, found))
    r.step()
    r._late = 1.0
    n, sent = int(0.1 * pcm.SAMPLE_RATE), clock.at
    for i in range(190):                                      # 19 s: one whole slot and a bit
        sent += 0.1
        clock.at = sent + 1.0
        r.feed(np.zeros((n, pcm.CHANNELS), np.int16))
        r.step()
    return told


def test_only_a_receiver_of_the_owners_own_is_reported() -> None:
    found = [{"snr": 3.0, "dt": 0.5, "hz": 1000, "text": "CQ K1ABC FN42"}, {"snr": 1.0, "dt": 0.5, "hz": 700, "text": "TNX BOB 73 GL"}]
    own = "http://192.168.50.4:8075"
    [(mode, dial, t0, told)] = _reader(found, own, 14_074_000, own + "/stream?freq=14074000")
    assert (mode, dial, t0) == ("ft8", 14_074_000, T0) and [(m["call"], m["grid"], m["hz"]) for m in told] == [("K1ABC", "FN42", 1000)]
    assert _reader(found, None, 14_074_000, own + "/stream") == []                          # a receiver on the internet: never
    assert _reader(found, own, None, own + "/stream") == []                                  # nobody said where it's tuned
    assert _reader(found, own, 14_074_000, "http://kiwi.example:8073/?f=14074usb") == []     # the radio has gone on to something else
    assert _reader(found, own, 14_074_000, None) == []


def test_reporting_from_the_web(tmp_path: Path, monkeypatch) -> None:
    class Station:
        source = {"url": "http://192.168.50.4:8075/stream?f=1", "name": "Short wave"}

    conf = tmp_path / "config.toml"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Station(), None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    decoder = tmp_path / "decode_ft8"
    decoder.write_text("#!/bin/sh\n")
    decoder.chmod(0o755)
    monkeypatch.setenv("SLEEPRADIO_FT8_DECODER", str(decoder))

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    monkeypatch.setattr(report.reporter, "_send", lambda packet: None)
    monkeypatch.setattr(ft8.reader, "report", report.reporter.heard)        # (as main.py joins them)
    monkeypatch.setattr(report.reporter, "_post", lambda query: None)
    try:
        monkeypatch.setattr(room, "CALLSIGN", None)
        monkeypatch.setattr(ft8, "HOME", "IO94")
        st = call("/api/rx/report")[1]
        assert st["on"] is False and st["can"] is False and st["now"] is False
        code, err = call("/api/rx/report", {"on": True})
        assert code == 400 and "callsign" in err["error"] and not report.reporter.on         # nobody to report as
        monkeypatch.setattr(room, "CALLSIGN", "M0ABC")
        assert call("/api/rx/report", {"on": "yes"})[0] == 400 and call("/api/rx/report", {})[0] == 400
        code, st = call("/api/rx/report", {"on": True})
        assert code == 200 and st["on"] is True and st["callsign"] == "M0ABC" and st["locator"] == "IO94" and st["sent"] == {"psk": 0, "wspr": 0}
        assert json.loads(conf.read_text())["report_heard"] is True
        # the reader told where the receiver is: one of the owner's own, by the radio's word for what's playing
        assert call("/api/rx/ft8", {"on": True, "band": "20 m", "dial": 14074000, "own": True})[0] == 200
        assert (ft8.reader.dial, ft8.reader.own) == (14_074_000, "http://192.168.50.4:8075") and call("/api/rx/report")[1]["now"] is True
        assert call("/api/rx/ft8")[1]["dial"] == 14_074_000
        assert call("/api/rx/ft8", {"on": True, "band": "20 m", "dial": 14074000})[0] == 200  # a receiver on the internet
        assert ft8.reader.own is None and call("/api/rx/report")[1]["now"] is False
        for bad in ("14074000", True, 5, 9e9):
            assert call("/api/rx/ft8", {"on": True, "band": "20 m", "dial": bad})[0] == 400
        assert call("/api/rx/ft8", {"on": True, "band": "20 m", "dial": 14074000, "own": True})[0] == 200
        assert call("/api/rx/ft8", {"on": True, "mode": "ft4"})[0] == 200 and ft8.reader.dial is None         # another mode: where it was tuned no longer holds
        code, st = call("/api/rx/report", {"on": False})
        assert code == 200 and st["on"] is False and json.loads(conf.read_text())["report_heard"] is False
    finally:
        report.reporter.switch(False)
        ft8.reader.switch(False)
        ft8.reader.playing, ft8.reader.mode, ft8.reader.band = None, "ft8", None       # (as it began, for the tests after)
        httpd.shutdown()
