"""Ambient mixer: combines noise + binaural output and layers it under
whatever Channel A (playback) is producing, then writes to the HiFiBerry
ALSA device via sounddevice.

Also owns the sleep timer's fade-and-stop of Channel A (ambient channels
keep playing once the timer fires -- mirrors SleepRadio's behaviour).
"""


class AmbientMixer:
    def __init__(self) -> None:
        raise NotImplementedError
