// Repair adapter for back panels printed before 2026-09-30, whose Pi post floors broke out
// (the head counterbore left a 0.2 mm wall at the post tip). A top hat: the
// stem pushes into the hollow post from the OUTSIDE, the flange sits on the
// outside face like a washer, and a self-tapper through the Pi bites into
// the stem, clamping the Pi onto the post rim.
//
//   openscad -o stl/post_adapter_test.stl post_adapter.scad            (3 widths, 1-3 notches)
//   openscad -D test=false -o stl/post_adapter.stl post_adapter.scad    (4 at stem_d; set count for fewer)

hole_d   = 5.6;    // the post's bore (as modelled)
depth    = 9;      // outside face to the solid post rim (5 mm panel + 7 mm post - 3 mm tip gone)
stem_len = depth - 0.5;   // a little short, so the Pi clamps on the rim, not the stem
stem_d   = 5.2;     // 5.2 fits the printed posts; 5.35 and 5.5 did not
pilot_d  = 2.2;    // M2.5 self-tapper into PLA (prints ~2.0)
flange_d = 10;
flange_t = 1.2;
test     = true;
count    = 4;      // how many for the non-test print
test_d   = [5.2, 5.35, 5.5];
$fn = 64;

module adapter(d, notches = 0) difference() {
    union() {
        cylinder(d = flange_d, h = flange_t);
        cylinder(d = d, h = flange_t + stem_len - 0.4);
        translate([0, 0, flange_t + stem_len - 0.4])       // lead-in chamfer
            cylinder(d1 = d, d2 = d - 0.8, h = 0.4);
    }
    translate([0, 0, -1]) cylinder(d = pilot_d, h = flange_t + stem_len + 2);
    for (i = [0 : notches - 1]) if (notches > 0)            // size marks on the flange rim
        rotate(i * 25) translate([flange_d / 2, 0, -1]) cylinder(d = 1.4, h = flange_t + 2, $fn = 12);
}

if (test)
    for (i = [0 : len(test_d) - 1]) translate([i * (flange_d + 4), 0, 0]) adapter(test_d[i], i + 1);
else
    for (i = [0 : count - 1]) translate([i * (flange_d + 4), 0, 0]) adapter(stem_d);
