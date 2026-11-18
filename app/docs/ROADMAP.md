# Roadmap

What's built is described in the [app README](../README.md) and the
[appliance image's README](../../README.md); this is what's left, the same list as
the image README's [Roadmap](../../README.md#roadmap) (keep the two in step).

- **Radios that sync: what's left** — paired radios already keep their
  settings, stations, themes, playlists, programmes, messages, birthdays,
  podcasts, DJ lines, theme jingles, voice packs and Wi-Fi networks the same
  both ways (*Other radios*), and the music, On demand, audiobooks and ebooks
  when shared, and Other radios shows counts, status and progress for each.
  What's shared is chosen item by item (the settings in four groups), with
  warnings where one item needs another, syncing can be paused, places in
  books and podcasts can be shared, and the window lists the recent syncs.
  Still to do: a radio with a web password pairing with its partners (today
  such a radio stays out of sync).
- **More ways to pick the sound** — the sound by kind of audio is built (music, radio, audiobook,
  ebook, podcast). Buttons can carry a sound too (day and night sets each have their own). Still open: a sound per
  music genre (needs reliable tags), a Music/Talk tag for stations played from the page, and the same on
  the cathedral radio (its selector would pick the sound).
- **Ebooks on a Pi Zero 2 W** — the Flite voices need 20-25 MB, so a Zero may be able to read
  ebooks after all (the neural voice ran it out of memory). Still to do: read a long book for an hour
  on a Zero with the station running (watch free memory, the first sound, the gaps), then let
  `ebooks: auto` turn on for a Zero when a Flite voice is the reading voice; and see whether a
  more natural small voice (a better Flite voice, MBROLA) is worth the work.
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
  in The world outside (a barometer; earthquakes, warnings, PSK Reporter
  spots of your own callsign, your QRZ logbook, lightning, aircraft from a local dump1090 and the 433 MHz devices from the receiver's background scan are done).
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
- **Frequency display + usable bass and treble + hearing check** (DONE 2026-11-15, 1.9.88 + app 1.4.0:
  desktop Speakers → Frequency display; the app's Frequencies, Bass and treble with a loudness budget, and the
  hearing check, remembered per radio). On Phonos the Balanced result sounds about the same as Rock.
- **An easier volume control on the desktop page** (DONE 2026-11-15: − and + keys either side of the volume
  under the knob, a hold keeps going; the knob stays as the indicator, click / scroll as before)
- **Aircraft in 3D** (DONE 2026-11-17, 1.9.92: a Flat / 3D switch above the Aircraft scope; the 3D view stands each
  aircraft on a stalk over the map with its recent path; the Aircraft window now updates itself while it's open)
- **Which aircraft was that alert?** (DONE 2026-11-18, 1.9.102: in 3D an alerted aircraft has a ▲ banner from the start and its
  details say what the alert is; an alert that has ended stays in the Aircraft window for 10 minutes as EARLIER, with the callsign)
- **DJ lines: fully managed** (DONE 2026-11-18, 1.9.100: edit, remove, switch off and restore every
  DJ line, your own and the 70s ones; changes sync between paired radios that share themes.)
- **Tune the speakers from the phone app** (DONE 2026-11-15: radio side 1.9.87, app 1.1+;
  the app measures with a sweep from three places, suggests, Listen / Adjust / Save) — the speaker analyser (the web page at
  `/analyser/`: quiet, then hiss, measured by the phone's microphone, suggesting the EQ and low cut)
  built into the Android remote (SleepRadioRemote) as a native screen: no Chrome flag or https needed,
  the app plays the tune sound, measures, and applies or undoes the suggestion on the radio's EQ.
  Chunky controls like the rest of the app. (Asked for 2026-11-15.)
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
