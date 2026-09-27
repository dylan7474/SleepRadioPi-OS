// SleepRadioPi cathedral radio: a CONCEPT picture only (not printable parts).
// 1930s cathedral set, art deco: an arched cabinet with a sunburst fretwork
// grille over one 4-inch speaker, a stepped dial window with the VU meter,
// a name plaque, fluted pilasters and one knob.
//
// Frame: x across (0 = centre), y front to back (y = 0 is the cabinet's
// front face; trim stands out towards -y), z up. Millimetres.
//
//   ./make-concept.sh   (renders concept.jpg)

/* ---------- Size and colours ---------- */
W = 300;                 // width
H = 360;                 // height at the crown
D = 200;                 // depth
spring = 200;            // where the sides turn into the arch
arch_off = 15;           // arch circle centres at +-this: a slightly pointed arch

cabinet_colour = "#2f6fd6";   // blue, as the first radio
trim_colour    = "#fbfbf8";   // white face, mouldings, knob
cloth_colour   = "#c9a86a";   // gold grille cloth
meter_colour   = "#f3e2b8";   // warm, backlit meter face
letter_colour  = "#2f6fd6";
dark           = "#1b1b1f";
red            = "#c0392b";

font = "Fira Sans Compressed:style=SemiBold";   // tall and narrow, deco-ish; a real deco face later

$fn = 96;

/* ---------- Helpers ---------- */
// Extrude an (x, z) profile along y from y0 to y1.
module yext(y0, y1) translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children();

// The cabinet's front outline: straight sides, then the arch.
module outline2d() {
    R = W / 2 + arch_off;
    union() {
        translate([-W / 2, 0]) square([W, spring + 0.01]);
        intersection() {
            translate([-arch_off, spring]) circle(r = R);
            translate([arch_off, spring]) circle(r = R);
            translate([-W / 2, spring]) square([W, H]);
        }
    }
}
module inset2d(d) offset(delta = -d) outline2d();
module band2d(d0, d1) difference() { inset2d(d0); inset2d(d1); }

/* ---------- The grille ---------- */
grille_base = 175;                 // bottom of the arch grille
hub_r = 26;                        // half-sun at the bottom of the fan
module grille_area2d() intersection() { inset2d(44); translate([-W, grille_base]) square([2 * W, H]); }

// Art deco fan: rays from the half-sun, two arcs across, extra rays above each arc.
module fret2d() {
    rib = 6;
    module ray(a, r0) rotate(90 + a) translate([r0, -rib / 2]) square([H, rib]);
    intersection() {
        grille_area2d();
        translate([0, grille_base]) union() {
            circle(r = hub_r);
            for (a = [-75 : 25 : 75]) ray(a, 0);
            for (a = [-62.5 : 25 : 62.5]) ray(a, 70);
            for (a = [-68.75 : 12.5 : 68.75]) ray(a, 125);
            for (r = [70, 125]) difference() { circle(r = r + rib / 2); circle(r = r - rib / 2); }
        }
    }
    // a stepped frame round the opening
    difference() { offset(delta = 5) grille_area2d(); grille_area2d(); }
}

/* ---------- Parts of the picture ---------- */
module cabinet() {
    color(cabinet_colour) yext(0, D) outline2d();
    // plinth
    color(cabinet_colour) translate([-W / 2 - 8, -14, 0]) cube([W + 16, D + 22, 14]);
    color(trim_colour) translate([-W / 2 - 8, -14.5, 10]) cube([W + 16, 1, 3]);   // a thin white line along the plinth
}

module face() {
    color(trim_colour) {
        // the face plate, with the grille opening cut out
        yext(-3, 0) difference() { inset2d(10); translate([0, 14]) grille_area2d(); translate([-W, -1]) square([2 * W, 24]); }
        // stepped mouldings following the arch
        yext(-6, -3) difference() { band2d(10, 18); translate([-W, -1]) square([2 * W, 24]); }
        yext(-8, -6) difference() { band2d(24, 29); translate([-W, -1]) square([2 * W, 24]); }
        // the fretwork, flush with the face
        translate([0, 0, 14]) yext(-4, 0) fret2d();
    }
    // grille cloth behind it
    color(cloth_colour) translate([0, 0, 14]) yext(-1.4, -0.9) offset(delta = 2) grille_area2d();
}

// Fluted pilasters either side of the lower face.
pil_x = 108;
module pilasters() color(trim_colour) for (sx = [-1, 1]) translate([sx * pil_x, 0, 0]) {
    difference() {
        translate([-11, -10, 34]) cube([22, 10, 150]);
        for (fx = [-6, 0, 6]) translate([fx, -10, 44]) rotate([-90, 0, 0])
            hull() { cylinder(d = 3.2, h = 3); translate([0, -130, 0]) cylinder(d = 3.2, h = 3); }
    }
    translate([-15, -13, 26]) cube([30, 13, 9]);       // base block
    translate([-15, -13, 183]) cube([30, 13, 7]);      // capital
    translate([-12, -15, 189]) cube([24, 15, 5]);      // stepped cap
}

// Name plaque between the grille and the meter.
module plaque() {
    color(trim_colour) translate([0, -8, 157]) rotate([90, 0, 0]) linear_extrude(4)
        offset(r = 3) square([128, 18], center = true);
    color(letter_colour) translate([0, -12.4, 157]) rotate([90, 0, 0]) linear_extrude(0.8)
        text("SLEEP  RADIO", size = 13, font = font, halign = "center", valign = "center", spacing = 1.15);
}

// The VU meter in a stepped dial window.
meter_z = 106;
module dial() {
    color(trim_colour) translate([0, 0, meter_z]) {
        yext(-6, 0) difference() { offset(r = 6) square([88, 60], center = true); offset(r = 4) square([66, 46], center = true); }
        yext(-10, -6) difference() { offset(r = 4) square([76, 52], center = true); offset(r = 4) square([66, 46], center = true); }
    }
    translate([0, -3.2, meter_z]) {
        color(meter_colour) yext(-2, 0) offset(r = 4) square([66, 46], center = true);
        // scale: ticks on an arc round the pivot, red from 0 VU up (right)
        piv = -20; r = 30;
        for (i = [0 : 10]) {
            ang = -40 + i * 8;
            color(i >= 5 ? red : dark) translate([0, -2.4, piv]) rotate([0, ang, 0])
                translate([0, 0, r + (i % 5 == 0 ? 3.5 : 2.2)]) cube([i % 5 == 0 ? 1.2 : 0.8, 0.4, i % 5 == 0 ? 7 : 4.4], center = true);
        }
        // the arc under the ticks: dark, then red past 0 VU
        for (seg = [[-40, 0, dark], [0, 40, red]]) color(seg[2]) translate([0, 0, piv]) yext(-2.6, -2.2)
            intersection() {
                difference() { circle(r = r + 0.8); circle(r = r); }
                polygon([[0, 0], for (t = [seg[0] : 2 : seg[1]]) 60 * [sin(t), cos(t)]]);
            }
        color(dark) translate([0, -2.6, piv + 13]) rotate([90, 0, 0]) linear_extrude(0.3) text("VU", size = 6, font = font, halign = "center");
        // the needle, just into the red: the music is playing
        color(dark) translate([0, -3, piv]) rotate([0, 12, 0]) translate([-0.5, 0, 0]) cube([1, 0.5, r + 4]);
        color(dark) translate([0, -3, piv]) rotate([90, 0, 0]) cylinder(d = 6, h = 1);
    }
}

// One knob below the dial: stepped, fluted, with a pointer.
module knob() color(trim_colour) translate([0, -8, 47]) rotate([90, 0, 0]) {
    difference() {
        union() {
            cylinder(d = 50, h = 6);
            cylinder(d = 40, h = 18);
            translate([0, 0, 18]) cylinder(d1 = 40, d2 = 34, h = 5);
        }
        for (i = [0 : 23]) rotate(i * 15) translate([20.5, 0, 7]) cylinder(d = 3.4, h = 20, $fn = 16);
        translate([-1, 6, 21]) cube([2, 12, 5]);
    }
}

cabinet();
face();
pilasters();
plaque();
dial();
knob();
