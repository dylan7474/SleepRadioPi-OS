// A small test for the two-tone fronts (see twotone/ and the README's
// "Two-tone front"): "SLEEP" at the front's lettering size and font, the same
// 0.8 mm depth and the same filament changes, on a small tile that prints in
// about 9 minutes -- so the colour change can be tried before the 3.5-hour
// front panel.
//
// Printed face down like the front: the lettering is cut into the bottom face,
// mirrored so it reads the right way round when the tile is turned over. The
// two slots show what the grille's slots will look like.
//
//   openscad --backend=manifold -o stl/twotone_test.stl twotone_test.scad

tile   = [50, 16, 1.6];   // 1.6 mm: just over the second colour change (1.4 mm)
depth  = 0.8;             // = logo_depth in sleepradiopi_box.scad (4 layers at 0.2 mm)
size   = [36, 7.2];       // one line of the front's lettering (logo_w, logo_line_h())
font   = "Liberation Sans:style=Bold";   // = logo_font

$fn = 48;

difference() {
    linear_extrude(tile[2])
        offset(r = 2) offset(delta = -2) square([tile[0], tile[1]], center = true);
    // the lettering, cut into the face (the bottom)
    translate([-5, 0, -1]) linear_extrude(1 + depth)
        mirror([1, 0, 0]) resize(size) text("SLEEP", size = 10, font = font, halign = "center", valign = "center");
    // grille-like slots, right through
    for (i = [0 : 1])
        translate([16.5 + i * 3.6, -5, -1]) cube([1.6, 10, tile[2] + 2]);
}
