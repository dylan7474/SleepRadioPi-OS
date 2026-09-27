# SleepRadioPi cathedral radio (concept)

![Concept: a 1930s art deco cathedral radio with a sunburst grille, a VU meter as the dial and one knob](concept.jpg)

The next case for the radio: a **1930s cathedral set, art deco**. So far this
is **a concept picture only**. The printable parts come once the speaker and
the meter are here to measure.

- **About 300 W × 360 H × 200 D mm.**
- **Art deco front:** a sunburst fretwork grille in the arch, over grille
  cloth and **one 4-inch (100 mm) full-range speaker**.
- **The VU meter as the dial window:** a backlit 500 µA panel meter in a
  stepped bezel, driven from the Pi (hardware PWM) with the app's ballistics.
- **One knob** below it; fluted pilasters and a SLEEP RADIO plaque.
- **Multi-part, bolted or clipped together, no glue:**
  - the face in two halves, joined up a centre rib;
  - the arched hood in segments, joined with internal flanges and M3 screws;
  - the plinth;
  - the back panel carrying the Pi, amp and RTC as in the [box](../case/);
  - a clip-in grille-cloth frame;
  - screw-on trim, which can be printed in a second colour.

![Front view](concept_front.jpg)

**In wood-look filament:** a walnut cabinet with a cream face, the classic
1930s finish. Print the hood and plinth in wood-filled PLA or Polymaker
PolyWood, and the face, fretwork, bezel and knob in cream PLA for crisp detail.

![Concept in walnut and cream](concept_walnut.jpg)

`concept.scad` is the picture's model (not printable parts), and
`./make-concept.sh` renders the pictures (`WALNUT=1` for the walnut ones) (needs OpenSCAD + ImageMagick).
Colours and sizes are variables at the top.
