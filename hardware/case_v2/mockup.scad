// SleepRadioPi box v2: a coloured mock-up of the finished radio, for pictures
// only. Reuses the real parts from sleepradiopi_box_v2.scad unchanged.
// Render with make-mockup.sh, which also turns it into a JPEG.
// mockup_cut = true cuts the box open down the middle (front to back, just
// left of the centre wall) and shows the right half from the left: the Pi on
// its tray in the bay, the shelf, and a speaker in its sealed chamber.

include <sleepradiopi_box_v2.scad>
part = "none";

body_colour    = "#2f6fd6";   // tube
panel_colour   = "#fbfbf8";   // front and rear panels
knob_colour    = "#fbfbf8";
letter_colour  = "#2f6fd6";   // the two-tone front: the letters' floor and a line round the edge
speaker_colour = "#0b0b0c";
button_colour  = "#c9ccd1";   // brushed stainless
mockup_cut     = false;

band = [0.8, 1.6];
module band_slab() translate([-1000, band[0], -1000]) cube([2000, band[1] - band[0], 2000]);
module cutaway() if (mockup_cut) difference() { children(); translate([-3000 - 3, -1000, -1000]) cube([3000, 3000, 3000]); } else children();

cutaway() {
    color(body_colour) tube();
    color(panel_colour) difference() { front(logo = true); band_slab(); }
    color(letter_colour) intersection() { front(logo = true); band_slab(); }
    color(panel_colour) rear();
    color(speaker_colour) speakers_placed();
    color(knob_colour) knob_placed();
    color(button_colour) for (x = btn_x) translate([x, btn_y, H]) cylinder(d = btn_head_d, h = btn_head_h);
}
// Cut open: the electronics, and the speaker's back in the chamber
if (mockup_cut) {
    cutaway() { pi_stack_model(); encoder_model(); buttons_model(); for (p = spk_pos) speaker_model(p); }
}
