"""Is the system clock right?

A Pi has no battery-backed clock. Offline, it only knows the last time it
saved, which after a night unplugged can be hours out, so the station must
not read out the time, greet by time of day or schedule news from it.

SLEEPRADIOPI_CLOCK_FLAG names a file that exists once the time is known to
be right (the appliance image creates /run/time-synced when NTP sets the
clock). Without the variable, e.g. on a desktop, the clock is trusted.
"""

import os
from pathlib import Path

ENV = "SLEEPRADIOPI_CLOCK_FLAG"


def clock_trusted() -> bool:
    flag = os.environ.get(ENV)
    return not flag or Path(flag).exists()
