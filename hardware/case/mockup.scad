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
// The two-tone front: the letters are cut 0.8 mm into the face (logo_depth),
// and the 4 layers behind the face (0.8-1.6 mm) are printed in letter_colour --
// the letters' floor, and a line round the panel's edge and in the grilles.
band = [0.8, 1.6];
module band_slab() translate([-1000, band[0], -1000]) cube([2000, band[1] - band[0], 2000]);
if (mockup_logo) {
    color(panel_colour) difference() { front(logo = true, grille = mockup_grille); band_slab(); }
    color(letter_colour) intersection() { front(logo = true, grille = mockup_grille); band_slab(); }
} else
    color(panel_colour) front(logo = false, grille = mockup_grille);
color(panel_colour) rear();
color(speaker_colour) speakers_placed();
color(knob_colour) knob_placed(mockup_knob);
