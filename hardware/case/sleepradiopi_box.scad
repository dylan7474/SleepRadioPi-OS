// SleepRadioPi box: a 3D-printable stereo radio cabinet for two small round
// speakers (Gikfun EK1794, 40 mm, no screw holes), a Raspberry Pi Zero 2 W +
// HiFiBerry MiniAmp, a DS3231 RTC and an EC11 rotary encoder in the top.
// No display.
//
// Printed parts that screw together:
//   tube  - top, bottom and sides in one piece, open front and back
//   front - baffle with two grilles; each speaker drops into a locating collar
//           behind its grille and is held by hot glue or three clamp tabs
//   rear  - back panel; the Pi, amp and RTC mount on its inside face, so the
//           whole electronics tray comes out with the panel
//   knob  - for the encoder's 6 mm D-shaft
//   tabs  - speaker clamp tabs (6 + 1 spare)
//   tube_ring - a 20 mm slice of the tube (same section, corner bosses and
//           knob hole) to test the panels and encoder before the full tube
//
// Render a part:
//   openscad --backend=manifold -D 'part="tube"' -o tube.stl sleepradiopi_box.scad
// part = "tube" | "tube_ring" | "front" | "rear" | "knob" | "tabs" | "front_plate" |
//        "front_plate_noknob" |
//        "assembly" | "exploded" | "rear_inside" | "front_inside" | "check" | "check_pull"
//
// Coordinate frame: x across the box (0 = centre, +x = right seen from the
// front), y front to back (y = 0 is the front face), z up (0 = underside).
// Everything is in millimetres.

part = "assembly";

// Draft: thin, fast fit-test panels (1.2 mm plate, open grille holes, no
// countersinks). Only for the front/rear/draft_plate parts -- never the tube.
draft = false;

// Lettering engraved into the front face between the speakers. Engraved, not
// raised: the front prints face down, and raised letters would need the whole
// panel on supports. Try -D front_logo=true with part="front" / "front_plate".
front_logo    = false;
logo_lines    = ["SLEEP", "RADIO"];
logo_font     = "Liberation Sans:style=Bold";
logo_w        = 36;      // every line is stretched to this width
logo_gap      = 4;       // between lines
logo_depth    = 0.8;     // 4 layers at 0.2 mm

/* ---------- Speakers (CHECK WITH CALIPERS) ---------- */
spk_d        = 40;      // rim diameter (Gikfun's listing: 40 mm)
spk_rim_t    = 2;       // thickness of the flat front rim/flange   (UNVERIFIED)
spk_depth    = 22;      // front of the rim to the back of the magnet (UNVERIFIED)
spk_grille_d = 34;      // grille opening, a bit less than the rim so the rim seals
spk_fit      = 0.6;     // collar clearance on the diameter
collar_t     = 1.6;
clamp        = true;    // screw bosses for the clamp tabs (hot glue works without)
clamp_angles = [90, 210, 330];
clamp_r      = spk_d / 2 + collar_t + 3.2;  // boss centre radius
clamp_boss_d = 6.5;
clamp_boss_h = 6;       // boss height above the panel's inside face (screw grip)
clamp_pilot  = 2.1;     // M2.5 self-tapping (the M2.5 x 6 screws from the Pi kit)
tab_t        = 2;

/* ---------- Pi Zero 2 W + MiniAmp (datasheet values, as in the old case) ---------- */
pcb_l      = 65;
pcb_w      = 30;
pcb_t      = 1.6;
hole_in    = 3.5;
port_otg_u = 41.4;       // port centres along the long edge, from the SD end
port_pwr_u = 54;
amp_gap    = 12;         // 12 mm M2.5 standoffs between the Pi and the amp
term_u0    = 23.9;       // MiniAmp speaker terminal, along the port edge
term_u1    = 44.4;
term_h     = 10;

/* ---------- Box ---------- */
inner_w     = 146;
inner_h     = 85;
ring_len    = 20;        // "tube_ring": a short slice of the tube for a fit test
tube_len    = part == "tube_ring" ? ring_len : 90;  // inside depth, front panel to rear panel
wall        = 3;
panel_t     = draft ? 1.2 : 5;
grille_t    = draft ? panel_t : 2.4;  // grille thickness; the rest is a recess for the cone
spk_x       = 45;        // speaker centres at +-spk_x
corner_r    = 5;

boss_r      = 4.5;       // screw bosses in the tube corners, for the panels
boss_off    = 4.5;       // boss centre from the inner walls
pilot_d     = 2.5;       // M3 self-tapping screws into the bosses
pilot_len   = 16;
panel_screw_d = 3.4;
panel_csk_d   = 6.6;

lip_t       = 1.6;       // locating lip on each panel, fits inside the tube
lip_h       = 3;
lip_clr     = 0.3;

hex_flat    = 3.2;       // grille: hexagonal holes, flat to flat
hex_web     = 1.2;

/* ---------- Rear panel electronics ---------- */
pi_dx       = 0;         // Pi position: offset from the centre line
post_h      = 7;         // room under the Pi for header stubs + encoder wires
post_d      = 6;         // tip, under the Pi
post_d2     = 7.6;       // below the tip
pi_screw    = "outside"; // "outside": M2.5 x 6 from the back through the posts into
                         //            the 12 mm standoffs (heads sit in deep counterbores)
                         // "self_tap": M2.5 self-tappers into the posts from the Pi side
screw_clear_d = 2.8;
screw_head_d  = 5.6;
screw_floor   = 2;       // plastic under the screw head, at the post tip
pi_tap_d      = 2.2;

cable_dx    = 32;        // power cable slot: this far out from the Pi's port edge
cable_w     = 14;
cable_h     = 10;

rtc         = true;      // ZS-042 DS3231: a locating rim, fix with foam tape
rtc_w       = 22.5;
rtc_l       = 38.5;
rtc_gap     = 17;        // from the Pi's GPIO edge to the RTC rim

/* ---------- Encoder + knob ---------- */
enc_hole_d  = 7.6;       // EC11 M7 bushing (a little over, for the sideways print)
enc_teardrop = false;    // true = pointed top, for printers that sag on round holes
enc_pocket_d = 15;       // thin the top wall around it so the nut gets thread
enc_pocket  = 1;
knob_d      = 30;
knob_h      = 16;
shaft_d     = 6.15;
shaft_flat  = 4.65;
shaft_len   = 12;

$fn = 64;

/* ---------- Derived ---------- */
W  = inner_w + 2 * wall;
H  = inner_h + 2 * wall;
D  = tube_len + 2 * panel_t;
ti = panel_t;                 // tube front end (inside face of the front panel)
to = panel_t + tube_len;      // tube rear end (inside face of the rear panel)

spk_z   = H / 2;
spk_pos = [[-spk_x, spk_z], [spk_x, spk_z]];
collar_h = spk_rim_t - 0.4;   // a little under the rim, so the tabs clamp the speaker
function clamp_pts(p) = [for (a = clamp_angles) p + clamp_r * [cos(a), sin(a)]];

bx = inner_w / 2 - boss_off;
bz0 = wall + boss_off;
bz1 = H - wall - boss_off;
bosses = [[-bx, bz0, -1, -1], [bx, bz0, 1, -1], [-bx, bz1, -1, 1], [bx, bz1, 1, 1]];

enc_y = ti + tube_len / 2;

// Pi on the rear panel, long side vertical, SD end down, parts facing forward,
// ports on the +x edge.
pi_x0   = pi_dx - pcb_w / 2;
pi_z0   = H / 2 - pcb_l / 2;
pi_y    = to - post_h;               // Pi underside
pi_top  = pi_y - pcb_t;              // component side (faces the front)
amp_y   = pi_top - amp_gap;          // amp underside
amp_top = amp_y - pcb_t;
port_x  = pi_x0 + pcb_w;             // port edge
function pi_pt(u, v) = [port_x - v, pi_z0 + u];  // board coords -> (x, z)
pi_holes = [for (u = [hole_in, pcb_l - hole_in]) for (v = [hole_in, pcb_w - hole_in]) pi_pt(u, v)];
cable_pos = [port_x + cable_dx, pi_z0 + port_pwr_u];
rtc_pos   = [pi_x0 - rtc_gap - rtc_w / 2, H / 2];

echo(str("Outer size: ", W, " x ", D, " x ", H, " mm (w x d x h); inside ",
         inner_w, " x ", tube_len, " x ", inner_h, " = ",
         round(inner_w * tube_len * inner_h / 1e4) / 100, " L"));

// The speaker mounts must stay clear of the lip and the corner bosses.
lip_in = [inner_w / 2 - lip_clr - lip_t, inner_h / 2 - lip_clr - lip_t];
for (p = spk_pos) for (c = concat([p + [sign(p[0]) * (spk_d / 2 + collar_t), 0]],
                                  clamp ? clamp_pts(p) : [])) {
    r = norm(c - p) > spk_d / 2 + collar_t + 0.1 ? clamp_boss_d / 2 : 0;
    assert(abs(c[0]) + r < lip_in[0] - 0.5 && abs(c[1] - H / 2) + r < lip_in[1] - 0.5,
           "A speaker mount hits the panel lip: move spk_x or the clamp angles");
    for (b = bosses) assert(norm(c - [b[0], b[1]]) > r + boss_r + 1, "A speaker mount hits a corner boss");
}

/* ---------- Helpers ---------- */
module rrect(x0, z0, x1, z1, r) {
    r = max(r, 0.01);
    hull() for (x = [x0 + r, x1 - r], z = [z0 + r, z1 - r]) translate([x, z]) circle(r = r);
}
// Extrude an (x, z) profile along y from y0 to y1.
module yext(y0, y1) translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children();
// A cylinder along y.
module ycyl(x, z, y0, y1, d, d2 = undef)
    translate([x, y0, z]) rotate([-90, 0, 0]) cylinder(d1 = d, d2 = is_undef(d2) ? d : d2, h = y1 - y0);

module outer2d() rrect(-W / 2, 0, W / 2, H, corner_r);
module inner2d(inset = 0)
    rrect(-inner_w / 2 + inset, wall + inset, inner_w / 2 - inset, H - wall - inset,
          max(corner_r - wall - inset, 0.5));

// Corner boss profile: a circle hulled into the corner.
module boss2d(b, grow = 0) hull() {
    translate([b[0], b[1]]) circle(r = boss_r + grow);
    translate([b[0] + b[2] * (boss_off + grow), b[1] + b[3] * (boss_off + grow)]) square(0.02, center = true);
    translate([b[0] + b[2] * (boss_off + grow), b[1] - b[3] * grow]) square(0.02, center = true);
    translate([b[0] - b[2] * grow, b[1] + b[3] * (boss_off + grow)]) square(0.02, center = true);
}

/* ---------- Tube ---------- */
module teardrop2d(d) hull() {   // point towards +y (up while the tube prints)
    circle(d = d);
    translate([0, d / 2 * sqrt(2)]) square(0.02, center = true);
}

module enc2d(d) if (enc_teardrop) teardrop2d(d); else circle(d = d);

module tube() {
    difference() {
        union() {
            yext(ti, to) difference() { outer2d(); inner2d(); }
            for (b = bosses) yext(ti, to) boss2d(b);
        }
        for (b = bosses) {
            ycyl(b[0], b[1], ti - 1, ti + pilot_len, pilot_d);
            ycyl(b[0], b[1], to - pilot_len, to + 1, pilot_d);
        }
        // Encoder hole in the top, with a thinner patch of wall around it.
        translate([0, enc_y, H - wall - 1]) linear_extrude(wall + 2) enc2d(enc_hole_d);
        translate([0, enc_y, H - wall - 1]) linear_extrude(1 + enc_pocket) enc2d(enc_pocket_d);
    }
}

/* ---------- Panels (shared) ---------- */
// Built with the outside face at y = 0 and the inside face at y = panel_t,
// the lip sticking out to y = panel_t + lip_h.
module panel_blank() {
    yext(0, panel_t) outer2d();
    yext(panel_t - 0.01, panel_t + lip_h) difference() {
        inner2d(lip_clr);
        inner2d(lip_clr + lip_t);
        for (b = bosses) boss2d(b, 0.6);
    }
}
module panel_screw_holes() for (b = bosses) {
    ycyl(b[0], b[1], -1, panel_t + 1, panel_screw_d);
    if (!draft) ycyl(b[0], b[1], -0.01, (panel_csk_d - panel_screw_d) / 2, panel_csk_d, panel_screw_d);
}

/* ---------- Front panel ---------- */
hex_pitch = hex_flat + hex_web;
module grille2d() intersection() {
    circle(d = spk_grille_d);
    n = ceil(spk_grille_d / hex_pitch);
    for (j = [-n : n]) for (i = [-n : n])
        translate([(i + (j % 2) / 2) * hex_pitch, j * hex_pitch * sqrt(3) / 2])
            rotate(30) circle(d = hex_flat / cos(30), $fn = 6);
}

module front(logo = front_logo) {
    difference() {
        union() {
            panel_blank();
            for (p = spk_pos) translate([p[0], 0, p[1]]) {
                // Locating collar around the speaker rim
                yext(panel_t - 0.01, panel_t + collar_h) difference() {
                    circle(d = spk_d + spk_fit + 2 * collar_t);
                    circle(d = spk_d + spk_fit);
                }
            }
            if (clamp) for (p = spk_pos) for (c = clamp_pts(p))
                ycyl(c[0], c[1], panel_t - 0.01, panel_t + clamp_boss_h, clamp_boss_d);
        }
        panel_screw_holes();
        for (p = spk_pos) translate([p[0], 0, p[1]]) {
            yext(-1, panel_t + 1) if (draft) circle(d = spk_grille_d); else grille2d();
            yext(grille_t, panel_t + 1) circle(d = spk_grille_d);  // cone clearance
        }
        if (clamp) for (p = spk_pos) for (c = clamp_pts(p))
            ycyl(c[0], c[1], draft ? -1 : 1.2, panel_t + clamp_boss_h + 1, clamp_pilot);
        if (logo && !draft) yext(-1, logo_depth) logo2d();
    }
}

// The lettering, centred between the speakers, each line the same width
module logo2d() {
    n = len(logo_lines);
    line_h = logo_line_h();
    total = n * line_h + (n - 1) * logo_gap;
    for (i = [0 : n - 1])
        translate([0, spk_z + total / 2 - line_h / 2 - i * (line_h + logo_gap)])
            resize([logo_w, line_h])
                text(logo_lines[i], size = 10, font = logo_font, halign = "center", valign = "center");
}
// Cap height once a line is stretched to logo_w (Liberation Sans Bold caps
// are about 0.72 x size tall and ~0.72 x size wide per letter)
function logo_line_h() = logo_w / max([for (l = logo_lines) len(l)]) * 1.0;

/* ---------- Speaker clamp tab ---------- */
// Stepped tab: the plate sits on the tall boss with the screw in the slot, and
// a foot under the rounded nose reaches down and presses on the rim. The foot
// stops short of the boss, even with the tab pulled back along its slot.
// Local z = 0 is the foot's underside; the plate is on top.
tab_len  = clamp_r - spk_d / 2 + 2;    // reaches 2 mm over the rim
tab_drop = clamp_boss_h - collar_h;    // foot height: rim + 0.4 mm press
tab_foot_x = -(clamp_boss_d / 2 + 0.8 + 0.3);
module tab2d() difference() {
    hull() { circle(d = clamp_boss_d); translate([-tab_len + 2.5, 0]) circle(d = 5); }
    hull() for (dx = [-0.8, 0.8]) translate([dx, 0]) circle(d = 2.8);  // slot for the M2.5 screw
}
module tab() {
    translate([0, 0, tab_drop]) linear_extrude(tab_t) tab2d();
    linear_extrude(tab_drop + 0.01) intersection() {
        tab2d();
        translate([tab_foot_x - 20, -10]) square([20, 20]);
    }
}
module tab_print() translate([0, 0, tab_drop + tab_t]) mirror([0, 0, 1]) tab();  // plate down, foot up

/* ---------- Rear panel ---------- */
// Built in place (outside face at y = D), so the Pi coordinates line up.
module rear() {
    difference() {
        union() {
            translate([0, D, 0]) mirror([0, 1, 0]) panel_blank();
            // Narrow tip next to the board (clear of the header pins), thicker
            // below, so the wall round the screw-head counterbore is 1 mm.
            for (h = pi_holes) {
                ycyl(h[0], h[1], pi_y, pi_y + 3, post_d);
                ycyl(h[0], h[1], pi_y + 3 - 0.01, to + 0.01, post_d2);
            }
            if (rtc) yext(to - 3, to + 0.01) translate(rtc_pos) difference() {
                square([rtc_w + 2.4, rtc_l + 2.4], center = true);
                square([rtc_w, rtc_l], center = true);
                square([rtc_w - 6, rtc_l + 4], center = true);  // gaps for the header wires
            }
        }
        translate([0, D, 0]) mirror([0, 1, 0]) panel_screw_holes();
        for (h = pi_holes) {
            if (pi_screw == "outside") {
                ycyl(h[0], h[1], pi_y - 1, D + 1, screw_clear_d);
                ycyl(h[0], h[1], pi_y + screw_floor, D + 1, screw_head_d);
            } else
                ycyl(h[0], h[1], pi_y - 1, D - 1.2, pi_tap_d);
        }
        // Power cable slot (the micro-USB plug threads through it) + zip-tie slots
        yext(to - 1, D + 1) {
            translate(cable_pos) rrect(-cable_w / 2, -cable_h / 2, cable_w / 2, cable_h / 2, 3);
            for (dz = [-8, 8]) translate(cable_pos + [cable_w / 2 + 6, dz])
                square([5, 3], center = true);
        }
    }
}

/* ---------- Knob (print top face down) ---------- */
module knob() {
    difference() {
        union() {
            cylinder(d = knob_d - 1.2, h = knob_h);
            for (a = [0 : 360 / 36 : 359]) rotate([0, 0, a])
                translate([knob_d / 2 - 1.1, 0, 0]) cylinder(d = 1.8, h = knob_h, $fn = 12);
        }
        translate([0, 0, -0.01]) intersection() {
            cylinder(d = shaft_d, h = shaft_len);
            translate([-shaft_d / 2, -shaft_d / 2, 0]) cube([shaft_flat, shaft_d, shaft_len]);
        }
        // Hollow skirt around the shaft so the knob clears the nut
        translate([0, 0, -0.01]) difference() {
            cylinder(d = knob_d - 6, h = 4);
            cylinder(d = shaft_d + 4, h = 4);
        }
        translate([0, -0.7, knob_h - 1]) cube([knob_d / 2 - 3, 1.4, 1.2]);
    }
}

/* ---------- Part models for previews and checks ---------- */
module speaker_model(p) color("dimgray") translate([p[0], 0, p[1]]) {
    ycyl(0, 0, ti, ti + spk_rim_t, spk_d);
    ycyl(0, 0, ti + spk_rim_t, ti + spk_depth - 8, spk_d - 6, 20);   // basket
    ycyl(0, 0, ti + spk_depth - 8, ti + spk_depth, 24);
    // Solder tabs, placed between two clamp bosses
    rotate([0, -30, 0]) translate([spk_d / 2 - 5, ti + spk_rim_t + 1, -2]) cube([6, 4, 4]);
}
module tabs_in_place() color("orange") for (p = spk_pos) for (a = clamp_angles)
    translate([p[0], 0, p[1]]) rotate([0, -a, 0]) translate([clamp_r, ti + spk_rim_t, 0])
        rotate([-90, 0, 0]) tab();

module pi_stack_model() {
    module board(y, c) color(c) yext(y - pcb_t, y) translate([pi_x0, pi_z0]) square([pcb_w, pcb_l]);
    board(pi_y, "green");
    board(amp_y, "darkgreen");
    // GPIO header + socket along the far edge
    color("black") yext(amp_y, pi_top) translate([pi_x0 + 0.5, pi_z0 + 7]) square([5, 51]);
    color("black") yext(pi_y, pi_y + 3) translate([pi_x0 + 0.5, pi_z0 + 7]) square([5, 51]);  // pin stubs
    // Ports
    color("silver") for (u = [port_otg_u, port_pwr_u])
        yext(pi_top - 2.6, pi_top) translate([port_x - 5, pi_z0 + u - 4]) square([5.5, 8]);
    // Amp speaker terminal
    color("dimgray") yext(amp_top - term_h, amp_top)
        translate([port_x - 7.5, pi_z0 + term_u0]) square([7.9, term_u1 - term_u0]);
    // Standoffs
    color("gold") for (h = pi_holes) ycyl(h[0], h[1], amp_y, pi_top, 4.5, 4.5);
    // Micro-USB power plug + cable bend: keep-out volume
    color("white", 0.8) yext(pi_top - 8, pi_top + 1)
        translate([port_x - 0.5, pi_z0 + port_pwr_u - 5.5]) square([24, 11]);
    // RTC module (board + CR2032 holder behind it)
    if (rtc) color("steelblue") yext(to - 9, to)
        translate(rtc_pos) square([rtc_w - 0.4, rtc_l - 0.4], center = true);
}

module encoder_model() color("gray") translate([0, enc_y, 0]) {
    translate([-6.5, -6.5, H - wall - 7]) cube([13, 13, 7]);
    translate([-5, -4, H - wall - 12]) cube([10, 8, 5]);   // pins + wires
    translate([0, 0, H - wall - 7]) cylinder(d = 6, h = wall + 7 + 14);
    translate([0, 0, H - wall]) cylinder(d = 7, h = wall + 5);
}

module knob_in_place() color("orange") translate([0, enc_y, H + 2]) knob();

// Uncoloured versions for mockup.scad (it picks its own colours)
module knob_placed() translate([0, enc_y, H + 2]) knob();
module speakers_placed() for (p = spk_pos) translate([p[0], 0, p[1]]) {
    ycyl(0, 0, ti, ti + spk_rim_t, spk_d);
    ycyl(0, 0, ti + spk_rim_t, ti + spk_depth - 8, spk_d - 6, 20);
}

module models() {
    for (p = spk_pos) speaker_model(p);
    pi_stack_model();
    encoder_model();
}

/* ---------- Print orientation ---------- */
module tube_print()  rotate([90, 0, 0]) translate([0, -ti, 0]) tube();          // front end down
module front_print() rotate([90, 0, 0]) front();                               // face down
module rear_print()  translate([0, 0, D]) rotate([-90, 0, 0]) rear();          // outside face down
module knob_print()  translate([0, 0, knob_h]) rotate([180, 0, 0]) knob();
module tabs_print()  for (i = [0 : 6]) translate([i * (clamp_boss_d + 3), 0, 0]) rotate(90) tab_print();

if (part == "tube" || part == "tube_ring") tube_print();
else if (part == "front") front_print();
else if (part == "rear") rear_print();
else if (part == "knob") knob_print();
else if (part == "tabs") tabs_print();
else if (part == "draft_plate") {   // run with -D draft=true: both panels + tabs
    front_print();
    translate([0, 6, 0]) rear_print();
    for (i = [0 : 6]) translate([W / 2 + 12, -H / 2 - 27 + i * (clamp_boss_d + 3), 0]) tab_print();
}
else if (part == "front_plate") {   // front panel + knob + tabs on one bed
    front_print();
    translate([-W / 2 + knob_d / 2, knob_d / 2 + 6, 0]) knob_print();
    translate([-W / 2 + knob_d + 10, 12, 0]) tabs_print();
}
else if (part == "front_plate_noknob") {   // same, without the knob (print knob.scad in another colour)
    front_print();
    translate([-W / 2 + knob_d + 10, 12, 0]) tabs_print();
}
else if (part == "assembly") {
    color("burlywood") tube();
    color("tan") front();
    color("tan") rear();
    models();
    if (clamp) tabs_in_place();
    knob_in_place();
}
else if (part == "exploded") {
    color("burlywood", 0.9) tube();
    translate([0, -60, 0]) { color("tan") front(); for (p = spk_pos) speaker_model(p); if (clamp) tabs_in_place(); }
    translate([0, 90, 0]) { color("tan") rear(); pi_stack_model(); }
    encoder_model();
    knob_in_place();
}
else if (part == "rear_inside") { color("tan") rear(); pi_stack_model(); }
else if (part == "front_inside") {
    color("tan") front();
    speaker_model(spk_pos[1]);
    if (clamp) color("orange") for (a = clamp_angles)
        translate([spk_x, 0, spk_z]) rotate([0, -a, 0]) translate([clamp_r, ti + spk_rim_t, 0]) rotate([-90, 0, 0]) tab();
}
// Interference checks: each should come out empty (touching faces show up as
// zero-volume slivers, which is fine).
else if (part == "check") {
    intersection() { tube(); union() { front(); rear(); } }
    intersection() { union() { tube(); front(); rear(); } models(); }
    intersection() { union() { for (p = spk_pos) speaker_model(p); } union() { pi_stack_model(); encoder_model(); } }
    intersection() { pi_stack_model(); encoder_model(); }
    if (clamp) intersection() { tabs_in_place(); union() { tube(); front(); for (p = spk_pos) speaker_model(p); } }
}
// The rear panel with everything on it slides straight out of the back.
else if (part == "check_pull")
    intersection() {
        hull() for (dy = [0, tube_len + 20]) translate([0, dy, 0]) pi_stack_model();
        union() { tube(); encoder_model(); for (p = spk_pos) speaker_model(p); }
    }
