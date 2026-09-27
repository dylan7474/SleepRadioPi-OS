"""Internet radio streaming: HTTP/HLS URL playback + ICY now-playing
metadata, via python-vlc (libVLC natively understands ICY metadata events
and HLS, so this should need little glue code beyond wiring the callback).
"""

from dataclasses import dataclass


@dataclass
class RadioStation:
    name: str
    url: str


class RadioPlayer:
    def __init__(self) -> None:
        raise NotImplementedError

    def play(self, station: RadioStation) -> None:
        raise NotImplementedError

    def now_playing(self) -> str | None:
        """Latest ICY StreamTitle, if the stream provides one."""
        raise NotImplementedError
