// SleepRadioPi box: the encoder knob on its own, so it can be printed
// separately (e.g. in a different colour from the box).
//
// Same knob as part="knob" in sleepradiopi_box.scad -- keep the two in step if
// either changes. Fits an EC11 encoder's 6 mm D-shaft.
//
// Printed top face down (the pointer groove is on the bed side), no supports:
//   openscad --backend=manifold -o stl/knob.stl knob.scad
//
// Millimetres.

knob_d      = 30;        // overall diameter across the grip ribs
knob_h      = 16;
shaft_d     = 6.15;      // D-shaft bore (6 mm shaft + clearance)
shaft_flat  = 4.65;      // bore width across the flat
shaft_len   = 12;        // bore depth
skirt_h     = 4;         // hollow underneath so the knob clears the encoder nut

$fn = 64;

module knob() {
    difference() {
        union() {
            cylinder(d = knob_d - 1.2, h = knob_h);
            // 36 grip ribs round the edge
            for (a = [0 : 360 / 36 : 359]) rotate([0, 0, a])
                translate([knob_d / 2 - 1.1, 0, 0]) cylinder(d = 1.8, h = knob_h, $fn = 12);
        }
        // D-shaft bore
        translate([0, 0, -0.01]) intersection() {
            cylinder(d = shaft_d, h = shaft_len);
            translate([-shaft_d / 2, -shaft_d / 2, 0]) cube([shaft_flat, shaft_d, shaft_len]);
        }
        // Hollow skirt around the shaft so the knob clears the nut
        translate([0, 0, -0.01]) difference() {
            cylinder(d = knob_d - 6, h = skirt_h);
            cylinder(d = shaft_d + 4, h = skirt_h);
        }
        // Pointer groove on the top face
        translate([0, -0.7, knob_h - 1]) cube([knob_d / 2 - 3, 1.4, 1.2]);
    }
}

// Top face down on the bed
translate([0, 0, knob_h]) rotate([180, 0, 0]) knob();
