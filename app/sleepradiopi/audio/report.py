"""Reporting what's heard: to PSK Reporter and to WSPRnet.

Two websites collect who heard whom from receivers all over the world and
draw it on a map, so that a station can see where its signal is getting to:

  * pskreporter.info takes FT8 and FT4 (and much else): "M0ABC in IO94
    heard K1ABC on 14.0751 MHz at -12 dB".
  * wsprnet.org takes WSPR beacons, with their power and drift as well.

This sends them what the radio's reader (audio/ft8.py) reads. It gives the
owner's callsign and locator to a public website, so it is OFF until it's
switched on, and can't be switched on until both are set.

Only a receiver of the owner's own is reported: one in the house, where the
locator says it is. A receiver on the internet is somewhere else, and what
it hears is not what the owner's aerial hears; the page says which kind is
being listened to, and the reader checks the radio is still playing that
receiver before anything is passed here (ft8.Reader.reports()).

PSK Reporter (pskreporter.info/pskdev.html) is sent UDP datagrams in IPFIX:
a header, two descriptors saying how the records are laid out (sent with
the first three datagrams and then once an hour), one record for the
receiver and one for each station heard. Its rules, kept here:

  * a datagram no more often than every five minutes, and not on the clock:
    SEND_EVERY_S and up to JITTER_S more, from when the reporter began;
  * a station on a band no more often than every five minutes, and better
    once an hour if nothing has changed: AGAIN_AFTER_S;
  * always from the same UDP port, with the same random identifier.

WSPRnet is sent each spot by itself, an ordinary web request, as WSJT-X
does it.

`reporter` is the one the radio uses: main.py switches it by the setting,
the reader hands it what it reads, the web side asks how it's doing.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

PSK_HOST, PSK_PORT = "report.pskreporter.info", 4739
ENTERPRISE = 30351                   # PSK Reporter's own fields are numbered under this
RX_LINK, TX_LINK = 0x50E2, 0x50E3    # (what ties a record to its descriptor: any numbers from 256 up)
SEND_EVERY_S = 300.0
JITTER_S = 60.0
AGAIN_AFTER_S = 1800.0               # the same station on the same band: not again for this long
DESCRIPTORS_FIRST = 3                # the descriptors go with this many datagrams to begin with...
DESCRIPTORS_EVERY_S = 3300.0         # ...and then this often
DATAGRAM_MOST = 1200                 # bytes: well inside one packet anywhere
WAITING_MOST = 2000                  # spots kept for the next datagram (a band is a few hundred stations at most)
WSPR_URL = "http://wsprnet.org/post"
WSPR_WAITING_MOST = 200
WSPR_TIMEOUT_S = 20.0
TICK_S = 5.0
MODE_NAMES = {"ft8": "FT8", "ft4": "FT4", "wspr": "WSPR"}

SOFTWARE = "Sleep Radio"             # main.py adds the version


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")[:254]
    return bytes([len(raw)]) + raw


def _padded(body: bytes) -> bytes:
    return body + b"\0" * (-len(body) % 4)


def _field(number: int, length: int) -> bytes:
    """One of PSK Reporter's own fields in a descriptor: its number, how long (0xFFFF: it says itself)."""
    return struct.pack("!HHI", 0x8000 | number, length, ENTERPRISE)


# How the receiver's record is laid out: its callsign, locator and software.
RX_DESCRIPTOR = _padded(struct.pack("!HHHHH", 3, 36, RX_LINK, 3, 0) + _field(2, 0xFFFF) + _field(4, 0xFFFF) + _field(8, 0xFFFF))
# ...and a station heard: callsign, frequency (Hz), strength (dB), mode, locator, how it was got (1: read by a
# machine) and when (seconds by the clock).
TX_DESCRIPTOR = (struct.pack("!HHHH", 2, 60, TX_LINK, 7) + _field(1, 0xFFFF) + _field(5, 4) + _field(6, 1) + _field(10, 0xFFFF)
                 + _field(3, 0xFFFF) + _field(11, 1) + struct.pack("!HH", 150, 4))


def receiver_record(call: str, locator: str, software: str) -> bytes:
    body = _padded(_string(call) + _string(locator) + _string(software))
    return struct.pack("!HH", RX_LINK, 4 + len(body)) + body


def spot_record(spot: dict) -> bytes:
    """{"call", "hz" (on the air), "snr", "mode", "grid" ("" for none), "t"} -> its bytes."""
    return (_string(spot["call"]) + struct.pack("!I", int(spot["hz"]) & 0xFFFFFFFF) + struct.pack("!b", max(-128, min(127, round(spot["snr"]))))
            + _string(MODE_NAMES[spot["mode"]]) + _string(spot.get("grid") or "") + b"\x01" + struct.pack("!I", int(spot["t"])))


def datagrams(call: str, locator: str, software: str, spots: list[dict], now: float, sequence: int, ident: int, descriptors: bool) -> list[bytes]:
    """The datagrams that report these spots: as many as it takes to keep each under DATAGRAM_MOST. The sequence
    number counts the reports sent before each one."""
    out = []
    head = (RX_DESCRIPTOR + TX_DESCRIPTOR if descriptors else b"") + receiver_record(call, locator, software)
    records = [spot_record(s) for s in spots]
    while True:
        body, n = b"", 0
        while records and 16 + len(head) + 4 + len(body) + len(records[0]) + 3 <= DATAGRAM_MOST:
            body += records.pop(0)
            n += 1
        block = struct.pack("!HH", TX_LINK, 4 + len(_padded(body))) + _padded(body) if n else b""
        packet = head + block
        out.append(struct.pack("!HHIII", 0x000A, 16 + len(packet), int(now), sequence & 0xFFFFFFFF, ident) + packet)
        sequence += n
        if not records:
            return out


def wspr_query(call: str, locator: str, software: str, spot: dict) -> dict:
    """A WSPR spot as WSPRnet is sent it. {"call", "grid", "dial" (Hz), "hz" (on the air), "snr", "dt", "drift",
    "dbm", "t" (the slot's start)}."""
    when = time.gmtime(spot["t"])
    return {"function": "wspr", "rcall": call, "rgrid": locator, "rqrg": f"{spot['dial'] / 1e6:.6f}", "date": time.strftime("%y%m%d", when),
            "time": time.strftime("%H%M", when), "sig": f"{round(spot['snr'])}", "dt": f"{spot['dt']:.1f}", "drift": f"{int(spot.get('drift') or 0)}",
            "tqrg": f"{spot['hz'] / 1e6:.6f}", "tcall": spot["call"], "tgrid": spot["grid"], "dbm": f"{int(spot['dbm'])}", "version": software[:10], "mode": "2"}


def _post_wspr(query: dict) -> None:
    req = urllib.request.Request(WSPR_URL + "?" + urllib.parse.urlencode(query), headers={"User-Agent": "SleepRadio"})
    with urllib.request.urlopen(req, timeout=WSPR_TIMEOUT_S) as r:
        r.read(2000)


class Reporter:
    """Takes what the reader reads (heard()) and sends it on, at the websites' pace. who() says whose report it
    is: (callsign, locator), either None if it isn't set."""

    def __init__(self, who: Callable[[], tuple[str | None, str | None]], clock: Callable[[], float] = time.time,
                 send: Callable[[bytes], None] | None = None, post: Callable[[dict], None] = _post_wspr) -> None:
        self.on = False
        self._who, self._clock, self._post = who, clock, post
        self._send = send or self._udp
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._waiting: dict[tuple[str, int], dict] = {}        # (call, MHz) -> the spot to send next time
        self._sent_at: dict[tuple[str, int], float] = {}       # ...and when each was last sent
        self._wspr: list[dict] = []
        self._ident = struct.unpack("!I", os.urandom(4))[0]
        self._sequence = 0
        self._datagrams = 0
        self._described = 0.0
        self._due = 0.0
        self.sent = {"psk": 0, "wspr": 0}                      # reports sent since it was switched on
        self.last: dict | None = None                          # {"t", "to": "psk" | "wspr", "n" | "error"}

    # --- what the reader hands over ----------------------------------------------------------

    def heard(self, mode: str, dial: int, t0: float, found: list[dict]) -> None:
        """A slot's messages from a receiver of the owner's own, tuned to dial (Hz): each {"call", "hz" (in the
        sound), "snr", "grid"?, and for WSPR "dt", "drift", "dbm"}. Ones with no call are nobody's."""
        if not self.on or mode not in MODE_NAMES:
            return
        now = self._clock()
        with self._lock:
            for m in found:
                call = m.get("call")
                if not call:
                    continue
                hz = int(dial) + int(m["hz"])
                if mode == "wspr":
                    if m.get("grid") and m.get("dbm") is not None and len(self._wspr) < WSPR_WAITING_MOST:
                        self._wspr.append({"call": call, "grid": m["grid"], "dial": int(dial), "hz": hz, "snr": m["snr"], "dt": m.get("dt", 0.0),
                                           "drift": m.get("drift", 0), "dbm": m["dbm"], "t": t0})
                    continue
                key = (call, hz // 1_000_000)
                if now - self._sent_at.get(key, 0.0) < AGAIN_AFTER_S or (key not in self._waiting and len(self._waiting) >= WAITING_MOST):
                    continue
                had = self._waiting.get(key)
                self._waiting[key] = {"call": call, "hz": hz, "snr": m["snr"], "mode": mode, "grid": m.get("grid") or (had or {}).get("grid") or "", "t": t0}

    # --- the switch, and how it's doing ------------------------------------------------------

    def switch(self, on: bool) -> None:
        """On or off. ValueError: it can't be on without a callsign and a locator."""
        on = bool(on)
        if on:
            call, locator = self._who()
            if not call or not locator:
                raise ValueError("reporting needs your callsign and your locator first: it's you the report is from")
        with self._lock:
            if on == self.on:
                return
            self.on = on
            self._stop.set()
            self._waiting.clear()
            self._wspr.clear()
            if on:
                self.sent, self.last, self._datagrams = {"psk": 0, "wspr": 0}, None, 0
                self._due = self._clock() + SEND_EVERY_S + random.uniform(0.0, JITTER_S)
                self._stop = stop = threading.Event()
                threading.Thread(target=self._run, args=(stop,), name="reporter", daemon=True).start()

    def status(self) -> dict:
        """{"on", "can" (a callsign and a locator are set), "callsign", "locator", "waiting" (spots for the next
        datagram), "sent": {"psk", "wspr"}, "last": {"t", "to", "n" | "error"} | None, "next_s" (until the next datagram)}"""
        call, locator = self._who()
        with self._lock:
            return {"on": self.on, "can": bool(call and locator), "callsign": call, "locator": locator, "waiting": len(self._waiting) + len(self._wspr),
                    "sent": dict(self.sent), "last": self.last, "next_s": max(0, round(self._due - self._clock())) if self.on else None}

    # --- the sending -------------------------------------------------------------------------

    def _udp(self, packet: bytes) -> None:
        if self._sock is None:                       # (one socket for good: always the same port)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.sendto(packet, (PSK_HOST, PSK_PORT))

    def step(self) -> None:
        """Send what's due: WSPR spots as they come, the PSK Reporter datagram when its time is up."""
        call, locator = self._who()
        if not call or not locator:                  # (taken away since: nothing more is said)
            with self._lock:
                self._waiting.clear()
                self._wspr.clear()
            return
        now = self._clock()
        with self._lock:
            wspr, self._wspr = self._wspr, []
        for spot in wspr:
            try:
                self._post(wspr_query(call, locator, SOFTWARE, spot))
                with self._lock:
                    self.sent["wspr"] += 1
                    self.last = {"t": now, "to": "wspr", "n": 1}
            except (OSError, ValueError) as e:
                with self._lock:
                    self.last = {"t": now, "to": "wspr", "error": str(e)[:120]}
                log.warning("report: WSPRnet didn't take a spot (%s)", e)
        with self._lock:
            if now < self._due:
                return
            self._due = now + SEND_EVERY_S + random.uniform(0.0, JITTER_S)
            spots = sorted(self._waiting.values(), key=lambda s: s["t"])
            if not spots:
                return
            keys = list(self._waiting)
            self._waiting.clear()
            describe = self._datagrams < DESCRIPTORS_FIRST or now - self._described >= DESCRIPTORS_EVERY_S
            packets = datagrams(call, locator, SOFTWARE, spots, now, self._sequence, self._ident, describe)
        try:
            for packet in packets:
                self._send(packet)
        except OSError as e:
            with self._lock:
                self.last = {"t": now, "to": "psk", "error": str(e)[:120]}
            log.warning("report: PSK Reporter wasn't reached (%s)", e)
            return
        with self._lock:
            self._sequence += len(spots)
            self._datagrams += len(packets)
            if describe:
                self._described = now
            for key in keys:
                self._sent_at[key] = now
            for key in [k for k, t in self._sent_at.items() if now - t > 2 * AGAIN_AFTER_S]:
                del self._sent_at[key]
            self.sent["psk"] += len(spots)
            self.last = {"t": now, "to": "psk", "n": len(spots)}

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_S):
            try:
                self.step()
            except Exception:                        # (a report that falls over mustn't end the reporter)
                log.exception("report: sending failed")


def _owner() -> tuple[str | None, str | None]:
    from sleepradiopi.audio import ft8
    return ft8.own_call(), ft8.HOME


reporter = Reporter(_owner)
