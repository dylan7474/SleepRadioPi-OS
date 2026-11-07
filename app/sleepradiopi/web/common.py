"""What the web handler's modules share: limits, patterns and small helpers (the handler itself is server.py; its
helper methods, by area, are routes_*.py)."""

from __future__ import annotations

import re

IDLE_CLOSE_S = 30  # close a stream connection that has had no audio for this long
MAX_SETTINGS_BYTES = 512 * 1024   # room for a long list of internet radio stations

# A receiver of your own, asked through the radio (/api/rx/own/...): whose it is (the address a saved station plays
# from) and what may be asked of one -- what the page asks, no more (receiver/sleepradio_receiver.py answers it).
OWN_RX = re.compile(r"(https?://[^/\s]+)/audio\?(?:.*&)?(?:band|freq)=")
OWN_RX_ASKS = {"GET": ("status", "bands", "spectrum"), "POST": ("skip", "tune", "hold", "bands")}

# Set where something restarts the station when it exits (init's respawn on the
# appliance image, systemd's Restart= on Raspberry Pi OS). Without it, e.g. on
# a desktop, loaded settings that need a restart wait for the next start.
RESTART_ENV = "SLEEPRADIOPI_SUPERVISED"

NO_EBOOKS = "this radio hasn't the memory to read ebooks aloud (a Pi 3 or better can)"


def _quietly(job) -> None:
    try:
        job()
    except Exception:            # (logged where it failed)
        pass
