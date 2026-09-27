// SleepRadioPi box: a coloured mock-up of the finished radio, for pictures only.
// Reuses the real parts from sleepradiopi_box.scad unchanged.
// Render with make-mockup.sh, which also turns it into a JPEG.

use <sleepradiopi_box.scad>

body_colour    = "#2f6fd6";   // tube (top, bottom, sides)
panel_colour   = "#fbfbf8";   // front and rear panels
knob_colour    = "#fbfbf8";   // the knob, printed separately
speaker_colour = "#0b0b0c";   // cones seen through the grilles
mockup_logo    = false;       // show the SLEEP RADIO lettering on the front
mockup_grille  = "hex";       // "hex" or "sunburst"
mockup_knob    = "plain";     // "plain" or "sunburst"

color(body_colour) tube();
color(panel_colour) front(logo = mockup_logo, grille = mockup_grille);
color(panel_colour) rear();
color(speaker_colour) speakers_placed();
color(knob_colour) knob_placed(mockup_knob);
