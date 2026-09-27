"""GPIO button input, via gpiozero.

Pin -> action mapping lives in config, not here (see
sleepradiopi.config.settings) -- e.g. 4 preset buttons (mirroring
SleepRadio's preset slots 1-4), play/pause, skip, volume up/down, sleep
timer.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ButtonMapping:
    pin: int
    action: str  # e.g. "preset_1", "play_pause", "skip", "vol_up", "sleep_timer"


class ButtonController:
    def __init__(self, mapping: list[ButtonMapping]) -> None:
        self.mapping = mapping

    def on_action(self, action: str, callback: Callable[[], None]) -> None:
        """TODO: wire a gpiozero.Button per mapping entry, debounce, and
        dispatch to callback on press.
        """
        raise NotImplementedError
