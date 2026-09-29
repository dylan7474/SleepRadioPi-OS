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
3. **More streaming** — the 📻 Streaming view is meant to grow: next,
   playing albums from the radio's own library there (straight through, no
   DJ); maybe choosing saved stations from the knob; the DJ's time checks
   or news over a station (off for now: a station plays untouched).
4. **A needle VU meter** — a physical meter driven from a PWM pin, with the
   Android app's ballistics (see the [appliance image's roadmap](../../README.md#roadmap)).
