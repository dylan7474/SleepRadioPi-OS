// SleepRadioPi box: a coloured mock-up of the finished radio, for pictures only.
// Reuses the real parts from sleepradiopi_box.scad unchanged.
// Render with make-mockup.sh, which also turns it into a JPEG.

use <sleepradiopi_box.scad>

body_colour    = "#2f6fd6";   // tube (top, bottom, sides)
panel_colour   = "#fbfbf8";   // front and rear panels
knob_colour    = "#fbfbf8";   // the knob, printed separately
letter_colour  = "#2f6fd6";   // the bottom of the SLEEP RADIO letters: the two-tone front
                              // (twotone/front_white_face_blue_letters.3mf); set to
                              // panel_colour for a one-colour print
speaker_colour = "#0b0b0c";   // cones seen through the grilles
mockup_logo    = false;       // show the SLEEP RADIO lettering on the front
mockup_grille  = "hex";       // "hex" or "sunburst"
mockup_knob    = "plain";     // "plain" or "sunburst"

color(body_colour) tube();
color(panel_colour) front(logo = mockup_logo, grille = mockup_grille);
// The letters are cut 0.8 mm into the face (logo_depth); colour their floor.
if (mockup_logo) color(letter_colour) yext(0.8 - 0.1, 0.8) logo2d();
color(panel_colour) rear();
color(speaker_colour) speakers_placed();
color(knob_colour) knob_placed(mockup_knob);
