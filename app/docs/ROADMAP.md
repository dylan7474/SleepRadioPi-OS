# Roadmap

**Direction (2026-09-22): Broadcast Radio is the product.** The other
SleepRadio sources (audiobooks, ambient noise and binaural beats) are
deferred and may never be built; their placeholder modules stay in the tree
until that's decided. **Internet radio came back (2026-09-29)** as the page's
*Streaming* view (📻).

What's built is described in the [README](../README.md); this is what's
left.

## Next

1. **Wire the knob and the RTC** — check the knob's direction and step and
   the long press on the real switch; that a press wakes the radio after
   the sleep timer has faded it out and paused it (playing again, speaker
   back at its normal level, the page showing it); the RTC with no network.
2. **Library tools** — copying/syncing music from the desktop library.
3. **Fit the preset buttons** — the code, wiring (GPIO5/6/16/26) and case
   holes are done (2026-09-29): drill the printed tube with `button_guide`,
   wire four 12 mm buttons, and try press / hold on the real thing. Then
   maybe: LEDs showing the playing preset (4 more pins), the DJ's time checks
   or news over a station (off for now: a station plays untouched).
4. **A needle VU meter** — a physical meter driven from a PWM pin, with the
   Android app's ballistics (see the [appliance image's roadmap](../../README.md#roadmap)).
