// SleepRadioPi box: a coloured mock-up of the finished radio, for pictures only.
// Reuses the real parts from sleepradiopi_box.scad unchanged (included, not
// used, so its settings -- the buttons -- can be set here; part = "none" stops
// it drawing anything itself).
// Render with make-mockup.sh, which also turns it into a JPEG.

include <sleepradiopi_box.scad>
part = "none";
buttons = mockup_buttons;     // the four preset buttons in the top (and the knob moved back)

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
mockup_handle  = false;       // the leather carry strap and its side loops
mockup_buttons = true;        // the four preset buttons along the front of the top
button_colour  = "#c9ccd1";   // brushed stainless
strap_colour   = "#6b4226";   // leather
stud_colour    = "#b08d57";   // brass Chicago screws

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
// The buttons' flat stainless heads (the rest is inside)
if (mockup_buttons) color(button_colour) for (x = btn_x) translate([x, btn_y, H])
    cylinder(d = btn_head_d, h = btn_head_h);

// The carry strap, as it hangs when carried: up from each loop and over the top
// in an arch. (W, H, D and the loop sizes come from sleepradiopi_box.scad.)

s_ztop = H - loop_top; s_zbot = s_ztop - loop_h;
s_x = W / 2 + loop_gap / 2;                       // strap centre, in the channel
s_xo = W / 2 + loop_t + strap_t / 2;              // ...folded back up over the bar
s_z0 = H + 2;                                     // where the arch begins
s_b = 80;                                         // the arch's height
module sect(x, z, ang = 0) translate([x, D / 2, z]) rotate([0, ang, 0]) cube([strap_t, strap_w, 0.05], center = true);
if (mockup_handle) {
    color(panel_colour) strap_loops_placed();
    color(strap_colour) for (sx = [-1, 1]) mirror([sx < 0 ? 1 : 0, 0, 0]) {
        // down through the loop, round the bottom and back up over the bar
        hull() { sect(s_x, s_zbot - 6); sect(s_x, s_z0); }
        hull() { sect(s_x + strap_t / 2 + 1, s_zbot - 9, 90); sect(s_xo - 1, s_zbot - 9, 90); }
        hull() { sect(s_xo, s_zbot - 7); sect(s_xo, s_ztop); }
        hull() { sect(s_xo, s_ztop); sect(s_x + strap_t, s_ztop + 7); }
        hull() { sect(s_x + strap_t, s_ztop + 7); sect(s_x + strap_t, s_ztop + 19); }
    }
    color(stud_colour) for (sx = [-1, 1]) mirror([sx < 0 ? 1 : 0, 0, 0])
        translate([s_x + strap_t * 1.5, D / 2, s_ztop + 13]) rotate([0, 90, 0]) cylinder(d = 8, h = 1.2);
    color(strap_colour) for (i = [0 : 35]) {
        t0 = i * 5; t1 = t0 + 5;
        hull() {
            sect(s_x * cos(t0), s_z0 + s_b * sin(t0), -t0);
            sect(s_x * cos(t1), s_z0 + s_b * sin(t1), -t1);
        }
    }
}
