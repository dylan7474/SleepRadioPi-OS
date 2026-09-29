# Roadmap

**Direction (2026-09-22): Broadcast Radio is the product.** The other
SleepRadio sources (binaural beats) are deferred and may never be built
(coloured **noise** came back on 2026-09-29, layered on top of everything); their placeholder modules stay in the tree
until that's decided. **Internet radio came back (2026-09-29)** as the page's
Streaming view, **audiobooks** on 2026-09-29, and **podcasts** the same day
(search, follow, episodes, saved places, and a button that plays a run of
episodes in order), sharing the audiobooks' saved-place and seek plumbing.

What's built is described in the [README](../README.md); this is what's
left.

## Next

1. **Wire the knob and the RTC** — check the knob's direction and step and
   the long press on the real switch; that a press wakes the radio after
   the sleep timer has faded it out and paused it (playing again, speaker
   back at its normal level, the page showing it); the RTC with no network.
2. **Library tools** — the web page's *Media library* (Settings) adds and
   deletes music and jingles (2026-09-29). Still to do: syncing a whole
   desktop library in one go (e.g. an rsync-style script from the PC).
3. **Fit the preset buttons** — the code, wiring (GPIO5/6/16/26) and case
   holes are done (2026-09-29): 16 mm buttons ordered; try them in
   `top_test`, print `tube_buttons` (knob moved back), wire them, and try
   press / hold on the real thing, and the service menu (1 and 4 held for
   15 s; built 2026-09-29, tried so far from the page). Also worth a thought before the cathedral:
   a screw-on lid, so changing the controls means reprinting a lid, not the
   whole ~5-hour tube. Then
   maybe: LEDs showing the playing preset (4 more pins), the DJ's time checks
   or news over a station (off for now: a station plays untouched).
4. **A needle VU meter** — a physical meter driven from a PWM pin, with the
   Android app's ballistics (see the [appliance image's roadmap](../../README.md#roadmap)).
