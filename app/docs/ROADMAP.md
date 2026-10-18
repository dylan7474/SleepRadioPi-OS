# Roadmap

What's built is described in the [app README](../README.md) and the
[appliance image's README](../../README.md); this is what's left, the same list as
the image README's [Roadmap](../../README.md#roadmap) (keep the two in step).

- **A needle VU meter** — a physical meter driven from a PWM pin, with the
  Android app's ballistics; one meter first.
- **A dongle inside the radio** — the bigger radios (a Pi 3 inside) have
  the room and the processor for an RTL-SDR of their own: one with a
  temperature-compensated crystal (a Nooelec NESDR SMArt v5), so that it
  stays on frequency without calibrating, as a receiver that's simply
  there in Receivers with no second computer.
- **Morse across the band: wider** — the Morse reader reads every signal
  in what's playing (up to four, across the couple of kHz a sideband
  gives) and lists who's been heard. Still to do: a KiwiSDR's wide "IQ"
  mode, about 10 kHz at once (the radio then has to make the sound you
  hear itself); each signal named by its callsign on the waterfall; and
  the heard list kept by the radio, not only in the page.
- **Band conditions: more of it** — *How the bands are* says it from the
  last survey and the last hour's listening, in the Bands window or by
  the DJ, against the same hour on the days before, with the weather
  teleprinters' strength as a second yardstick, N0NBH's forecast,
  AuroraWatch UK and alerts. Still to do: where your own receiver's
  reports put you among others', once *Report* is on; more from outside
  in The world outside (your QRZ logbook; earthquakes, warnings and PSK
  Reporter spots of your own callsign are done).
- **A laser-cut wood cathedral** — a real-wood version of the cathedral case:
  - the face and fretwork cut from walnut-veneered plywood, the sides and
    back from plywood or MDF;
  - the arch kerf-bent (cut so the plywood bends) or built from stacked
    profiles;
  - printed parts where that's easier (knob, meter bezel, speaker ring,
    brackets);
  - the flat parts exported as DXF/SVG for the laser from the same OpenSCAD
    design.
- **Messages from away: a recorded clip** — the messages can now be kept
  in a document the radio fetches (see *Messages from away*). Maybe a
  recorded clip, in your own voice, as well as the DJ's reading.
- **A glow behind the grilles** — warm-white LEDs (a 5 V COB strip)
  lighting the grille from behind, for a soft glow at night: dimmable from the
  page, fading with the sleep timer, shared with the VU meter's backlight. On
  the box it glows round each speaker (a clip-in holder inside the collar,
  wired with the speakers). On the cathedral radio it's behind the grille
  cloth, so the sunburst shows in silhouette.
- **A cathedral radio** — a 1930s art deco cathedral set in wood (a ported
  4-inch speaker, the VU meter as its dial, a six-way selector) is now its own
  project, **Phonosphere** (private for now). It runs this software; its
  differences (six presets, the selector, a back button, the meter and the
  grille light) are the `"cathedral"` hardware profile here (software done;
  the image's GPIO keys and PWM next — see *Two radios* in the app README).
- **Auto-tune with a USB mic** — a cheap USB mic in the Zero's data port,
  where you sit, and an **Auto-tune** button on the radio's page:
  - it plays the pink noise, records the box and works out the EQ and low
    cut with the analyser's maths (and `audio/eq.py`);
  - it applies them, measures again, and goes back to the old settings if
    they didn't help;
  - no phone or laptop needed; re-run it after changing the back panel or
    moving the radio (a new cathedral box especially);
  - needs USB audio in the trimmed kernel.
- **A carry strap** — designed, still to print and fit: a leather strap
  through two printed side loops, with a drill guide for a case that's already
  printed (see [the case README](../../hardware/case/README.md#carry-strap)).
