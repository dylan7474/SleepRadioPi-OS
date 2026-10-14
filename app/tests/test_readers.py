"""Kept over a restart: the reader for receivers that was on (audio/readers.py), and where the receiver was tuned
(Station.retuned). The whole of it, through a real restart, is in test_startup.py."""
import threading

from sleepradiopi.audio import readers
from sleepradiopi.broadcast.station import Station


class _Reader:
    def __init__(self, mode=None) -> None:
        self.on, self.mode, self.band, self.dial, self.own, self.playing = False, mode, None, None, None, None
        self.asked = []

    def switch(self, on, mode=None, band=None, dial=None, own=None) -> None:
        if mode not in (None, "ft8", "ft4", "wspr", "psk31", "rtty", "sstv", "fax"):
            raise ValueError("no such mode")
        self.asked.append((on, mode, band, dial, own))
        self.on, self.mode = on, mode or self.mode


def _fakes(monkeypatch) -> dict:
    out = {"cw": _Reader(), "ft8": _Reader("ft8"), "keyboard": _Reader("psk31"), "pictures": _Reader("sstv")}
    for name, module in (("cw", readers.morse), ("ft8", readers.ft8), ("keyboard", readers.keyboard), ("pictures", readers.pictures)):
        monkeypatch.setattr(module, "reader", out[name])
    monkeypatch.setattr(readers.ft8, "can", lambda mode: True)
    return out


def test_what_is_on_is_said_and_switched_on_again(monkeypatch) -> None:
    r = _fakes(monkeypatch)
    assert readers.state() is None
    r["keyboard"].switch(True, "rtty")
    assert readers.state() == {"reader": "keyboard", "mode": "rtty"}
    r["keyboard"].on = False
    r["ft8"].on, r["ft8"].mode, r["ft8"].band, r["ft8"].dial, r["ft8"].own = True, "ft4", "20 m", 14_080_000, "http://rx:8075"
    kept = readers.state()
    r = _fakes(monkeypatch)                          # (a restart)
    playing = lambda: "http://rx:8075/audio?freq=14.080000&mode=usb"
    assert readers.restore(kept, playing) == "ft8"
    assert r["ft8"].asked == [(True, "ft4", "20 m", 14_080_000, "http://rx:8075")] and r["ft8"].playing is playing
    assert readers.restore({"reader": "cw"}) == "cw" and r["cw"].on


def test_what_doesnt_make_sense_is_left_off(monkeypatch) -> None:
    r = _fakes(monkeypatch)
    for kept in (None, "keyboard", {}, {"reader": "radar"}, {"reader": "keyboard", "mode": "olivia"}):
        assert readers.restore(kept) is None
    assert not any(x.on for x in r.values())
    monkeypatch.setattr(readers.ft8, "can", lambda mode: False)        # (a card without the decoder)
    assert readers.restore({"reader": "ft8", "mode": "wspr"}) is None


def _station(source) -> tuple[Station, list]:
    st = Station.__new__(Station)
    saved = []
    st._lock, st._source, st.on_source = threading.RLock(), source, saved.append
    return st, saved


def test_a_receiver_tuned_while_playing_keeps_where() -> None:
    src = {"kind": "radio", "name": "60 metres", "url": "http://rx:8075/audio?band=60m"}
    st, saved = _station(src)
    assert st.retuned("http://rx:8075", 18_096_400, "usb")
    assert src["url"] == "http://rx:8075/audio?freq=18.096400&mode=usb" and saved[-1]["url"] == src["url"]     # in place, and saved
    assert st.retuned("http://rx:8075/", 18_096_400, "usb") and len(saved) == 1                               # the same: not saved again
    assert not st.retuned("http://rx:8074", 145_500_000, "nfm")                # another receiver than the one playing
    for freq, mode in ((None, "usb"), (True, "usb"), (18e6, None), (18e6, "u s b"), (9e9, "usb")):
        assert not st.retuned("http://rx:8075", freq, mode)
    for other in (None, {"kind": "album", "folder": "x"}, {"kind": "radio", "name": "BBC", "url": "http://bbc.example/live"}):
        assert not _station(other)[0].retuned("http://rx:8075", 18_100_000, "usb")
