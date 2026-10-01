// SleepRadioPi box: the encoder knob on its own, so it can be printed
// separately (e.g. in a different colour from the box).
//
// Same knob as part="knob" in sleepradiopi_box.scad -- keep the two in step if
// either changes. Fits an EC11 encoder's 6 mm D-shaft, 20 mm long from the bushing's base.
//
// Printed top face down (the pointer groove is on the bed side), no supports:
//   openscad --backend=manifold -o stl/knob.stl knob.scad
// Sunburst version (the front grilles' fan engraved in the top, rising towards
// where the groove would point):
//   openscad --backend=manifold -D 'knob_style="sunburst"' -o stl/knob_sunburst.stl knob.scad
//
// Millimetres.

knob_d      = 30;        // overall diameter across the grip ribs
knob_h      = 20;
shaft_d     = 6.15;      // D-shaft bore (6 mm shaft + clearance)
shaft_flat  = 4.65;      // bore width across the flat
shaft_len   = 16.5;      // bore depth: a 20 mm EC11 shaft stands ~18 mm above the
                         // case (2 mm wall at the pocket), so the knob clears it by ~1.5 mm
nut_recess_d = 12;       // under the knob, round the encoder's M7 nut + washer
nut_recess_h = 3;
skirt_h     = 4;         // hollow underneath so the knob clears the encoder nut

knob_style  = "plain";   // "plain" (pointer groove) or "sunburst"
// Sunburst: the grille's fan scaled to the knob (same numbers as
// sleepradiopi_box.scad: rays over +-80 degrees, half-sun and arc at 8/34 and
// 20/34 of the diameter, an extra ray between each pair above the arc)
fan_d       = 26;
fan_rays    = 7;
fan_spread  = 80;
fan_rib_w   = 1.4;
engrave     = 0.8;

$fn = 64;

module knob() {
    difference() {
        union() {
            cylinder(d = knob_d - 1.2, h = knob_h);
            // 36 grip ribs round the edge
            for (a = [0 : 360 / 36 : 359]) rotate([0, 0, a])
                translate([knob_d / 2 - 1.1, 0, 0]) cylinder(d = 1.8, h = knob_h, $fn = 12);
        }
        // Shaft bore, D at the top, and the nut recess
        translate([0, 0, -0.01]) intersection() {
            cylinder(d = shaft_d, h = shaft_len);
            translate([-shaft_d / 2, -shaft_d / 2, 0]) cube([shaft_flat, shaft_d, shaft_len]);
        }
        translate([0, 0, -0.01]) cylinder(d = nut_recess_d, h = nut_recess_h);
        // Hollow skirt around the shaft so the knob clears the nut
        translate([0, 0, -0.01]) difference() {
            cylinder(d = knob_d - 6, h = skirt_h);
            cylinder(d = shaft_d + 4, h = skirt_h);
        }
        if (knob_style == "sunburst")
            translate([0, 0, knob_h - engrave]) linear_extrude(engrave + 0.01) rotate(-90) fan2d();
        else
            // Pointer groove on the top face
            translate([0, -0.7, knob_h - 1]) cube([knob_d / 2 - 3, 1.4, 1.2]);
    }
}

// The fan's open slots (engraved), rising along +y from a half-sun at the bottom
module fan2d() {
    hub_r = fan_d * 8 / 34;
    arc_r = fan_d * 20 / 34;
    step = 2 * fan_spread / (fan_rays - 1);
    module ray(a, r0) rotate(90 + a) translate([r0, -fan_rib_w / 2]) square([fan_d * 2, fan_rib_w]);
    translate([0, -fan_d / 2]) difference() {
        translate([0, fan_d / 2]) circle(d = fan_d);
        circle(r = hub_r);
        for (i = [0 : fan_rays - 1]) ray(-fan_spread + i * step, 0);
        for (i = [0 : fan_rays - 2]) ray(-fan_spread + (i + 0.5) * step, arc_r);
        difference() { circle(r = arc_r + fan_rib_w / 2); circle(r = arc_r - fan_rib_w / 2); }
    }
}

// Top face down on the bed
translate([0, 0, knob_h]) rotate([180, 0, 0]) knob();
