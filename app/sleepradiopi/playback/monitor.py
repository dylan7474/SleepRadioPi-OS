"""Monitor: rooms heard over whatever is playing.

A room (room.py) is quiet nearly all the time, so tuning one in as the
station means mostly silence. Monitored instead, the radio carries on with
its music, a station or a book, sits in the room in the background, and
when someone speaks there the programme dips and the room comes through;
then the programme comes back. Up to MAX_ROOMS rooms at once: the first to
speak has the air until its over ends.

The station passes every block it's about to play through mix(); with
nobody talking that's the block itself, untouched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.playback import radio, room

log = logging.getLogger(__name__)

MAX_ROOMS = 4
DUCK = 0.10                  # the programme's level under a room (-20 dB): the "music under a room" setting, as a fraction
                             # (main.py and the web side set it; 0 = silent under a voice, 1 = not turned down at all)
JITTER_S = 0.3               # this much of an over is in hand before it starts: the internet delivers in lumps
HOLD_S = 1.2                 # the programme stays down this long after the last word, for the reply
MAX_BUFFER_S = 4.0           # speech waiting to be played: more than this and the oldest goes (the radio was paused)
RETRY_S = 30.0               # a room that dropped is joined again after this


def clean_rooms(rooms) -> list[dict]:
    """[{"name", "url"}] of rooms, each once, MAX_ROOMS at most. ValueError if one isn't a room."""
    out, seen = [], set()
    for r in rooms or []:
        st = radio.validate_station(r)
        if not room.is_room(st["url"]):
            raise ValueError(f"{st['name']}: only a room can be monitored")
        if st["url"] not in seen:
            seen.add(st["url"])
            out.append({"name": st["name"], "url": st["url"]})
    if len(out) > MAX_ROOMS:
        raise ValueError(f"{MAX_ROOMS} rooms at most can be monitored at once")
    return out


class Monitor:
    def __init__(self, rooms=(), open_room: Callable = room.RoomStream, clock: Callable[[], float] = time.monotonic) -> None:
        self._open, self._clock = open_room, clock
        self._lock = threading.RLock()
        self.rooms: list[dict] = []
        self._streams: dict = {}                    # url -> the room, joined
        self._ended: dict[str, float] = {}          # url -> when it dropped
        self._buf: list[np.ndarray] = []
        self._buffered = 0
        self._active: str | None = None             # the room that has the air
        self._playing = False
        self._last_speech = -1e9
        self._gain = 1.0
        self.set_rooms(rooms)

    # (which rooms)

    def set_rooms(self, rooms) -> list[dict]:
        rooms = clean_rooms(rooms)
        with self._lock:
            for url in [u for u in self._streams if u not in {r["url"] for r in rooms}]:
                self._streams.pop(url).close()
            self.rooms = rooms
        return rooms

    def restart(self) -> None:
        """Join them afresh (the callsign changed)."""
        with self._lock:
            for url in list(self._streams):
                self._streams.pop(url).close()
            self._ended.clear()

    def close(self) -> None:
        self.set_rooms([])

    def state(self, url: str) -> dict | None:
        with self._lock:
            st = self._streams.get(url)
            return {**st.rig(), "monitor": True} if st is not None else None

    def status(self) -> dict:
        with self._lock:
            name = {r["url"]: r["name"] for r in self.rooms}
            act = self._streams.get(self._active) if self._active else None
            return {"rooms": [{**r, "talker": getattr(self._streams.get(r["url"]), "talker", None),
                               "joined": r["url"] in self._streams} for r in self.rooms],
                    "talking": {"call": act.talker, "room": name.get(self._active, "")} if act is not None and act.talker else None}

    # (the sound)

    def _keep(self, playing_url: str | None, now: float) -> None:
        """Each monitored room joined -- except one that's the station itself just now (it's heard anyway,
        and two connections under one callsign would trip over each other)."""
        for r in self.rooms:
            url, st = r["url"], self._streams.get(r["url"])
            if url == playing_url:
                if st is not None:
                    self._streams.pop(url).close()
                continue
            if st is not None and st.ended is not None:
                log.info("monitor: %s: %s", r["name"], st.ended)
                self._streams.pop(url).close()
                self._ended[url] = now
                st = None
            if st is None and now - self._ended.get(url, -1e9) >= RETRY_S:
                st = self._streams[url] = self._open(url)
                st.start()

    def mix(self, block: np.ndarray, playing_url: str | None = None) -> np.ndarray:
        """The block the station is about to play, with a room over it if one is talking."""
        with self._lock:
            if not self.rooms and not self._streams and self._gain == 1.0:
                return block
            now = self._clock()
            self._keep(playing_url, now)
            if self._active is not None and (self._active not in self._streams or now - self._last_speech > HOLD_S) and not self._buffered:
                self._active, self._playing = None, False
            for url, st in self._streams.items():
                while (b := st.read(0)) is not None:
                    if not b.any():                           # (the room's own silence between overs)
                        continue
                    if self._active is None:
                        self._active = url
                    if url == self._active:
                        self._buf.append(b)                   # (at the level the room gives it: room.Shaper)
                        self._buffered += len(b)
                        self._last_speech = now
            while self._buffered > MAX_BUFFER_S * pcm.SAMPLE_RATE and len(self._buf) > 1:
                self._buffered -= len(self._buf.pop(0))
            n = len(block)
            # (enough in hand -- or a last short piece that nothing more is coming after: left waiting,
            # it would hold the programme down until the room next spoke)
            if not self._playing and self._buffered and (self._buffered >= JITTER_S * pcm.SAMPLE_RATE or now - self._last_speech > JITTER_S):
                self._playing = True
            speech = None
            if self._playing and n:
                parts, need = [], n
                while need and self._buf:
                    head = self._buf[0]
                    if len(head) <= need:
                        parts.append(self._buf.pop(0))
                    else:
                        parts.append(head[:need])
                        self._buf[0] = head[need:]
                    need -= len(parts[-1])
                    self._buffered -= len(parts[-1])
                if need:                                      # ran dry: the rest of the over is gathered before more is played
                    parts.append(np.zeros((need, pcm.CHANNELS), dtype=np.int16))
                    self._playing = False
                speech = np.concatenate(parts)
                self._last_speech = now                       # (the reply's wait counts from the last word played)
            want = DUCK if (self._playing or self._buffered or now - self._last_speech <= HOLD_S) else 1.0
            if speech is None and want == 1.0 and self._gain == 1.0:
                return block
            if not n:
                return block
            step = min(1.0, n / (0.15 * pcm.SAMPLE_RATE))         # (down or up over about a seventh of a second)
            to = self._gain + (want - self._gain) * step
            ramp = np.linspace(self._gain, to, n, dtype=np.float32)[:, None]
            self._gain = 1.0 if abs(to - 1.0) < 0.01 else to
            out = block.astype(np.float32) * ramp
            if speech is not None:
                out += speech
            return np.clip(out, -32768, 32767).astype(np.int16)
