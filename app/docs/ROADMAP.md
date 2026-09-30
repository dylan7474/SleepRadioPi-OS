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

0. **The new desktop interface** (the user's direction, 2026-09-30) — the web page
   becomes a desktop: icons for every thing (albums, tracks, stations,
   podcasts, books, playlists, programmes), windows as containers, the radio
   itself on the desktop (drop anything on its dial to play it, on a button to
   keep it there), settings kept on the thing they affect and on the radio's
   back panel, messages as sticky notes, a built-in media manager. Light theme
   first. The clickable prototype is `docs/prototypes/desktop.html`. Stages:
   1. **Programmes in the radio** — DONE 2026-09-30 (the scheduler, API, buttons).
   2. **The desktop served from the radio** — DONE 2026-09-30 (`/desktop`): the
      real library, playing, buttons, playlists and programmes; settings open
      the classic page's own in a window until stage 4.
   3. **The media manager in it** — DONE 2026-09-30: upload (files and folders,
      with progress), new folder, download (zip for folders), delete via the
      Trash, and move by dragging (music ↔ on demand; playlists, programmes and
      buttons follow); real lengths from the files for the timelines.
   4. **Every setting wired up** (speakers, knob, DJ, noise, Wi-Fi, buttons,
      voices, this radio, updates, save and load, power, messages, birthdays).
   5. **The switchover**: the desktop is the main page on a computer; this
      page stays as the phone remote.

1. **Wire the knob and the RTC** — done 2026-09-30 (amp unplugged): the knob's
   direction, one step a click, short press (pause) and long press all right;
   the RTC (a DS3231) sets the clock at boot over a power cut, before Wi-Fi.
   Still to check: that a press wakes the radio after the sleep timer has
   faded it out and paused it (needs the amp); the RTC over a night unplugged
   (it flags "oscillator stopped" at every boot, likely a clone chip or a
   tired CR2032, being replaced). The knob now has volume modes (Automatic:
   faster turns take bigger steps; Fine, Normal, Coarse; 2026-09-30): try
   Automatic on the real knob and tune `KNOB_ACCEL` if it jumps too much.
2. **Library tools** — the web page's *Media library* (Settings) adds and
   deletes music and jingles (2026-09-29). Still to do: syncing a whole
   desktop library in one go (e.g. an rsync-style script from the PC).
3. **Fit the preset buttons** — the code, wiring (GPIO5/6/16/26) and case
   holes are done (2026-09-29): 16 mm buttons ordered; try them in
   `top_test`, print `tube_buttons` (knob moved back), wire them, and try
   press / hold on the real thing, and the service menu (1 and 4 held for
   5 s; built 2026-09-29, tried so far from the page). Also worth a thought before the cathedral:
   a screw-on lid, so changing the controls means reprinting a lid, not the
   whole ~5-hour tube. Then
   maybe: LEDs showing the playing preset (4 more pins), the DJ's time checks
   or news over a station (off for now: a station plays untouched).
4. **A needle VU meter** — a physical meter driven from a PWM pin, with the
   Android app's ballistics (see the [appliance image's roadmap](../../README.md#roadmap)).
