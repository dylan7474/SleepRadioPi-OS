"""The cathedral radio's needle and glow: a VU meter and the grille's light.

Both are driven by the Pi's hardware PWM (config.txt: dtoverlay=pwm-2chan,
pin=12,func=4,pin2=13,func2=4 -> /sys/class/pwm/pwmchip0/pwm0 on GPIO12,
pwm1 on GPIO13; S40pwm exports them for the station's user).

- The **needle**: a 500 uA moving-coil VU meter through ~5.6 k + a 2 k
  trimmer from GPIO12 (the trimmer sets full scale at 3.3 V). A real VU
  movement already has the classic ballistics (it's the meter's own
  inertia), so it's fed the music's level, taken before the volume, lightly
  smoothed: the loudness-levelled music sits round 0 VU (duty 10^(-3/20) --
  full scale is +3 VU). Paused, or silent, it falls back to rest.
- The **glow**: warm-white LEDs round the speaker behind the cloth, through
  a MOSFET from GPIO13. Its own brightness for the day and the night sets of
  presets, a gamma curve so the steps look even, fading out over a few
  seconds on pause and with the sleep timer.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

PWM_ROOT = Path("/sys/class/pwm/pwmchip0")
PERIOD_NS = 100_000          # 10 kHz: far above what the needle or an eye can follow
TICK_S = 0.02                # 50 updates a second
GAMMA = 2.2
from sleepradiopi.audio import pcm
REF_MEAN_ABS = pcm.LOUDNESS_TARGET_RMS * 32767 * 0.8   # the levelled music's mean |sample|: 0 VU
GLOW_RAMP_S = 1.5            # brightness changes take about this long
NEEDLE_SMOOTH_S = 0.05


class Pwm:
    """One exported sysfs PWM channel. A missing one (a desktop, or the box
    radio) does nothing, quietly."""

    def __init__(self, channel: int, root: Path = PWM_ROOT, period_ns: int = PERIOD_NS) -> None:
        self.dir = Path(root) / f"pwm{channel}"
        self.period = period_ns
        self._duty: int | None = None
        self.ok = False
        try:
            (self.dir / "period").write_text(str(period_ns))
            (self.dir / "enable").write_text("1")
            self.ok = True
        except OSError as e:
            log.info("PWM %s not available (%s): that output is off", self.dir, e)

    def set(self, fraction: float) -> None:
        duty = int(round(max(0.0, min(1.0, fraction)) * self.period))
        if not self.ok or duty == self._duty:
            return
        try:
            (self.dir / "duty_cycle").write_text(str(duty))
            self._duty = duty
        except OSError as e:
            log.warning("PWM %s: %s", self.dir, e)
            self.ok = False


class Lamps:
    """The needle and the glow, updated TICK_S apart from the speaker's level."""

    def __init__(self, speaker, control, bank=lambda: "day", needle: Pwm | None = None, glow: Pwm | None = None,
                 glow_day: int = 60, glow_night: int = 15, meter_trim_db: float = 0.0) -> None:
        self.speaker, self.control, self.bank = speaker, control, bank
        self.needle = needle if needle is not None else Pwm(0)
        self.glow = glow if glow is not None else Pwm(1)
        self.glow_day, self.glow_night, self.meter_trim_db = glow_day, glow_night, meter_trim_db
        self._needle = 0.0
        self._glow = 0.0
        self._sweep_until = 0.0
        self._stop = threading.Event()

    # --- settings -------------------------------------------------------------------------

    def settings(self) -> dict:
        return {"glow_day": self.glow_day, "glow_night": self.glow_night, "meter_trim_db": self.meter_trim_db,
                "needle": self.needle.ok, "glow": self.glow.ok}

    def set(self, glow_day=None, glow_night=None, meter_trim_db=None) -> dict:
        for name, v, lo, hi in (("glow_day", glow_day, 0, 100), ("glow_night", glow_night, 0, 100),
                                ("meter_trim_db", meter_trim_db, -12, 12)):
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name} is {lo} to {hi}")
            setattr(self, name, round(float(v), 1) if name == "meter_trim_db" else int(v))
        return self.settings()

    def sweep(self, seconds: float = 4.0) -> None:
        """The needle up to full scale and back (set the trimmer so it just reaches the end)."""
        self._sweep_until = time.monotonic() + seconds

    # --- the loop -----------------------------------------------------------------------------

    def needle_target(self, level: float) -> float:
        """The needle's duty for a mean |sample| (pre-volume): 0 VU = 10^(-3/20)."""
        if level <= 0:
            return 0.0
        vu = 20 * math.log10(level / REF_MEAN_ABS) + self.meter_trim_db
        return min(1.0, 10 ** ((vu - 3) / 20))

    def glow_target(self) -> float:
        """The glow's brightness now (0-1, before the gamma curve)."""
        if self.control is not None and getattr(self.control, "paused", False):
            return 0.0
        pct = self.glow_night if self.bank() == "night" else self.glow_day
        return pct / 100 * getattr(self.speaker, "fade_factor", 1.0)

    def step(self, now: float, dt: float) -> None:
        if now < self._sweep_until:
            left = self._sweep_until - now
            phase = min(left, 4.0 - left) / 2.0              # up for 2 s, down for 2 s
            target = max(0.0, min(1.0, phase))
            self._needle = target
        else:
            level = getattr(self.speaker, "level", 0.0)
            if now - getattr(self.speaker, "level_at", 0.0) > 0.2:    # nothing playing: the needle falls
                level = 0.0
            target = self.needle_target(level)
            a = min(1.0, dt / NEEDLE_SMOOTH_S)
            self._needle += (target - self._needle) * a
        self.needle.set(self._needle)
        g = self.glow_target()
        step = dt / GLOW_RAMP_S
        self._glow = g if abs(g - self._glow) <= step else self._glow + math.copysign(step, g - self._glow)
        self.glow.set(self._glow ** GAMMA)

    def start(self) -> None:
        def run():
            last = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                try:
                    self.step(now, now - last)
                except Exception:
                    log.exception("lamps")
                last = now
                time.sleep(TICK_S)
        threading.Thread(target=run, name="lamps", daemon=True).start()
