// SleepRadioPi box, version 2: the cabinet designed around its speakers.
//
// Version 1 (../case) was sized to fit the parts; both speakers shared one
// leaky volume. Here each Gikfun EK1794 (40 mm, 4 ohm) gets its own sealed
// chamber, sized from the speaker's Thiele-Small figures (see README.md):
// with Qts ~1 and Vas ~0.1-0.3 L, a sealed box of 0.35-0.45 L per speaker is
// the sweet spot -- smaller loses bass, bigger gains nothing audible. A port
// doesn't suit a driver with Qts ~1.
//
//   top:    electronics bay -- the four buttons, the knob, and the Pi + amp
//           + RTC lying flat on a tray that comes out with the back panel;
//           the power lead plugs straight in through the back
//   below:  a sealed shelf, and under it two sealed chambers (one per
//           speaker) split by a centre wall; only the speaker wires pass
//           through the shelf (small holes, sealed with hot glue)
//
// The shelf and the centre wall are part of the tube (they print upright,
// front end down, like the walls) and run flush to both ends; the panels
// seal on them, and on the tube's rim, with 1-2 mm foam tape. Eight screws
// per panel: the four corners, the shelf's ends and the centre wall's ends.
//
// Printed parts:
//   tube  - top, bottom, sides, the shelf and the centre wall, open front and back
//   front - baffle with the two grilles, SLEEP RADIO in the top band
//   rear  - back panel with the electronics tray and the power plug hole
//   knob, tabs - as version 1
//
//   openscad --backend=manifold -D 'part="tube"' -o tube.stl sleepradiopi_box_v2.scad
// part = "tube" | "front" | "rear" | "knob" | "tabs" | "front_plate" |
//        "assembly" | "exploded" | "check" | "none"
//
// Coordinates as version 1: x across (0 = centre, +x right seen from the
// front), y front to back (0 = the front face), z up (0 = underside). mm.

part = "assembly";

/* ---------- Speakers (CHECK WITH CALIPERS, as version 1) ---------- */
spk_d        = 40;      // measured
spk_rim_t    = 2.7;     // measured
spk_depth    = 22;
spk_grille_d = 34;
spk_fit      = 0.6;
collar_t     = 1.6;
clamp_angles = [90, 210, 330];
clamp_r      = spk_d / 2 + collar_t + 3.2;
clamp_boss_d = 6.5;
clamp_boss_h = 6;
clamp_pilot  = 2.1;
tab_t        = 2;

/* ---------- Pi Zero 2 W + MiniAmp ---------- */
pcb_l      = 65;
pcb_w      = 30;
pcb_t      = 1.6;
hole_in    = 3.5;
port_otg_u = 41.4;       // along the long (port) edge, from the SD end
port_pwr_u = 54;
amp_gap    = 12;         // 12 mm M2.5 standoffs between the Pi and the amp
term_u0    = 23.9;       // MiniAmp speaker terminal, along the port edge
term_u1    = 44.4;
term_h     = 10;

/* ---------- Box ---------- */
inner_w     = 146;
chamber_h   = 66.6;      // bottom wall to the shelf: sets each chamber's volume
shelf_t     = 2.4;
bay_h       = 40;        // shelf to the top wall: the buttons' 30 mm bodies, the Pi stack
div_t       = 2.4;       // the centre wall between the chambers
tube_len    = 90;        // inside depth, front panel to rear panel
wall        = 3;
panel_t     = 5;
grille_t    = 2.4;
spk_x       = 37;        // speaker centres, in the middle of each chamber
corner_r    = 5;

boss_r      = 4.5;
boss_off    = 4.5;
pilot_d     = 2.5;       // M3 self-tapping screws into the bosses
pilot_len   = 16;
panel_screw_d = 3.4;
panel_csk_d   = 6.6;

lip_t       = 1.6;       // locating lip round each panel, inside the tube
lip_h       = 3;
lip_clr     = 0.3;

hex_flat    = 3.2;
hex_web     = 1.2;
grille_style = "sunburst";   // "hex" or "sunburst"
sun_rays    = 9;
sun_spread  = 80;
sun_hub_r   = 8;
sun_arc_r   = 20;
sun_rib_w   = 1.6;

// SLEEP RADIO across the top band of the front (in front of the bay)
front_logo  = true;
logo_lines  = ["SLEEP RADIO"];
logo_font   = "Liberation Sans:style=Bold";
logo_w      = 104;
logo_gap    = 4;
logo_depth  = 0.8;

/* ---------- Electronics tray (on the back panel) ---------- */
tray_t      = 3;
tray_gap    = 0.6;       // above the shelf
tray_len    = 46;        // how far it reaches forward from the back panel
post_h      = 3;         // under the Pi: room for the header's solder stubs
post_d      = 6;
pi_x0       = 4;         // the Pi's SD end (it runs to +x from here)
port_gap    = 2.5;       // the Pi's port edge to the back panel
screw_d     = 2.8;       // M2.5 x 10 from under the tray, into the 12 mm standoffs
screw_head_d = 5.6;
screw_cbore = 2;         // head counterbore depth, under the tray
plug_w      = 12;        // micro-USB plug moulding, through the back panel
plug_h      = 9;
rtc_w       = 38.5;      // ZS-042 DS3231, lying flat, long side across
rtc_l       = 22.5;
rtc_x0      = -70;
rtc_rim     = 1.5;       // a low rim to locate it (foam tape holds it)
wire_hole_d = 4;         // speaker wires through the shelf (seal with hot glue)
wire_x      = 22;        // at +-wire_x...
wire_y      = 44;        // ...this far from the front face

/* ---------- Encoder + knob, buttons (as version 1) ---------- */
enc_hole_d  = 7.6;
enc_pocket_d = 15;
enc_pocket  = 1;
knob_d      = 30;
knob_h      = 16;
knob_style  = "sunburst";
knob_sun_d  = 26;
knob_sun_rays = 7;
knob_sun_rib_w = 1.4;
knob_engrave = 0.8;
shaft_d     = 6.15;
shaft_flat  = 4.65;
shaft_len   = 12;
btn_hole_d  = 16.4;
btn_x       = [-39, -13, 13, 39];
btn_y       = 25;
enc_y       = 52;        // the knob, behind the buttons (from the front face)
btn_head_d  = 19;
btn_head_h  = 3;
btn_nut_d   = 22;
btn_body_d  = 18;
btn_body_h  = 30;

$fn = 64;

/* ---------- Derived ---------- */
inner_h = chamber_h + shelf_t + bay_h;
W  = inner_w + 2 * wall;
H  = inner_h + 2 * wall;
D  = tube_len + 2 * panel_t;
ti = panel_t;
to = panel_t + tube_len;
zs0 = wall + chamber_h;             // shelf underside
zs1 = zs0 + shelf_t;                // shelf top (the bay's floor)
spk_z = wall + chamber_h / 2;
spk_pos = [[-spk_x, spk_z], [spk_x, spk_z]];
collar_h = spk_rim_t - 0.4;
function clamp_pts(p) = [for (a = clamp_angles) p + clamp_r * [cos(a), sin(a)]];

// Screw bosses: [x, z, hull towards x, hull towards z]
bx = inner_w / 2 - boss_off;
bosses = [
    [-bx, wall + boss_off, -1, -1], [bx, wall + boss_off, 1, -1],    // bottom corners
    [-bx, H - wall - boss_off, -1, 1], [bx, H - wall - boss_off, 1, 1],  // top corners
    [-bx, zs0 - boss_off, -1, 1], [bx, zs0 - boss_off, 1, 1],        // the shelf's ends
    [0, wall + boss_off, 0, -1], [0, zs0 - boss_off, 0, 1]           // the centre wall's ends
];

// The tray and what's on it
zt0 = zs1 + tray_gap;               // tray underside
zt1 = zt0 + tray_t;                 // tray top
pi_z = zt1 + post_h;                // Pi underside
pi_top = pi_z + pcb_t;
amp_z = pi_top + amp_gap;           // amp underside
amp_top = amp_z + pcb_t;
pi_y1 = to - port_gap;              // the port edge (towards the back)
pi_y0 = pi_y1 - pcb_w;              // the GPIO edge
function pi_pt(u, v) = [pi_x0 + u, pi_y1 - v];   // board (u along, v in from the port edge) -> (x, y)
pi_holes = [for (u = [hole_in, pcb_l - hole_in]) for (v = [hole_in, pcb_w - hole_in]) pi_pt(u, v)];
plug_pos = [pi_x0 + port_pwr_u, pi_top + 1.3];   // (x, z) of the power socket's centre
rtc_y1 = to - 3;
rtc_pos = [rtc_x0 + rtc_w / 2, rtc_y1 - rtc_l / 2];

// Volumes
spk_back = PI * pow(18, 2) * (spk_depth - spk_rim_t) / 1e3;                 // cm^3, the speaker's back
chamber_gross = (inner_w / 2 - div_t / 2) * chamber_h * tube_len / 1e3;
chamber_bosses = 3 * PI * boss_r * boss_r * tube_len / 1e3 * 0.75;           // three per chamber, part-hulled
chamber_clamp = 3 * PI * pow(clamp_boss_d / 2, 2) * clamp_boss_h / 1e3;
chamber_net = chamber_gross - spk_back - chamber_bosses - chamber_clamp;
echo(str("Outer size: ", W, " x ", D, " x ", H, " mm (w x d x h); each speaker's chamber ",
         inner_w / 2 - div_t / 2, " x ", tube_len, " x ", chamber_h, " mm = ",
         round(chamber_net) / 1000, " L net (about ", round(chamber_net * 1.15) / 1000,
         " L effective with loose wadding)"));

// The speaker mounts must clear the lip, the shelf, the centre wall and the bosses.
for (p = spk_pos) for (c = concat([p + [sign(p[0]) * (spk_d / 2 + collar_t), 0],
                                   p + [-sign(p[0]) * (spk_d / 2 + collar_t), 0],
                                   p + [0, spk_d / 2 + collar_t], p + [0, -(spk_d / 2 + collar_t)]],
                                  clamp_pts(p))) {
    r = norm(c - p) > spk_d / 2 + collar_t + 0.1 ? clamp_boss_d / 2 : 0;
    assert(abs(c[0]) + r < inner_w / 2 - lip_clr - lip_t - 0.5, "A speaker mount hits the side lip");
    assert(abs(c[0]) - r > div_t / 2 + 0.5, "A speaker mount hits the centre wall");
    assert(c[1] - r > wall + lip_clr + lip_t + 0.5, "A speaker mount hits the bottom lip");
    assert(c[1] + r < zs0 - 0.5, "A speaker mount hits the shelf");
    for (b = bosses) assert(norm(c - [b[0], b[1]]) > r + boss_r + 1, "A speaker mount hits a screw boss");
}

/* ---------- Helpers ---------- */
module rrect(x0, z0, x1, z1, r) {
    r = max(r, 0.01);
    hull() for (x = [x0 + r, x1 - r], z = [z0 + r, z1 - r]) translate([x, z]) circle(r = r);
}
module yext(y0, y1) translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children();
module ycyl(x, z, y0, y1, d, d2 = undef)
    translate([x, y0, z]) rotate([-90, 0, 0]) cylinder(d1 = d, d2 = is_undef(d2) ? d : d2, h = y1 - y0);
module outer2d() rrect(-W / 2, 0, W / 2, H, corner_r);
module inner2d(inset = 0)
    rrect(-inner_w / 2 + inset, wall + inset, inner_w / 2 - inset, H - wall - inset,
          max(corner_r - wall - inset, 0.5));
module teardrop2d(d) hull() { circle(d = d); translate([0, d / 2 * sqrt(2)]) square(0.02, center = true); }
module btn2d(d) intersection() {
    teardrop2d(d);
    translate([-d, -d]) square([2 * d, d + min(d / 2 * sqrt(2), btn_head_d / 2 - 0.75)]);
}

// A boss: a circle hulled into the wall(s) it stands against.
module boss2d(b, grow = 0) hull() {
    translate([b[0], b[1]]) circle(r = boss_r + grow);
    if (b[2] != 0) translate([b[0] + b[2] * (boss_off + grow), b[1]]) square([0.02, 2 * (boss_r + grow)], center = true);
    if (b[3] != 0) translate([b[0], b[1] + b[3] * (boss_off + grow)]) square([2 * (boss_r + grow), 0.02], center = true);
    if (b[2] != 0 && b[3] != 0) translate([b[0] + b[2] * (boss_off + grow), b[1] + b[3] * (boss_off + grow)]) square(0.02, center = true);
}
// The inside walls, as a section: the shelf and the centre wall.
module walls2d(grow = 0) {
    translate([-inner_w / 2 - 1, zs0 - grow]) square([inner_w + 2, shelf_t + 2 * grow]);
    translate([-div_t / 2 - grow, wall - 1]) square([div_t + 2 * grow, zs0 - wall + 1.01]);
}

/* ---------- Tube ---------- */
module tube() {
    difference() {
        union() {
            yext(ti, to) difference() { outer2d(); inner2d(); }
            yext(ti, to) intersection() { walls2d(); inner2d(-0.01); }
            for (b = bosses) yext(ti, to) boss2d(b);
        }
        for (b = bosses) {
            ycyl(b[0], b[1], ti - 1, ti + pilot_len, pilot_d);
            ycyl(b[0], b[1], to - pilot_len, to + 1, pilot_d);
        }
        for (x = btn_x) translate([x, btn_y, H - wall - 1]) linear_extrude(wall + 2) btn2d(btn_hole_d);
        translate([0, enc_y, H - wall - 1]) linear_extrude(wall + 2) circle(d = enc_hole_d);
        translate([0, enc_y, H - wall - 1]) linear_extrude(1 + enc_pocket) circle(d = enc_pocket_d);
        // The speaker wires, one hole into each chamber (teardrop: prints sideways)
        for (sx = [-1, 1]) translate([sx * wire_x, wire_y, zs0 - 1])
            linear_extrude(shelf_t + 2) teardrop2d(wire_hole_d);
    }
}

/* ---------- Panels ---------- */
module panel_blank() {
    yext(0, panel_t) outer2d();
    yext(panel_t - 0.01, panel_t + lip_h) difference() {
        inner2d(lip_clr);
        inner2d(lip_clr + lip_t);
        for (b = bosses) boss2d(b, 0.6);
        walls2d(0.6);                         // notches where the shelf and centre wall meet the sides
    }
}
module panel_screw_holes() for (b = bosses) {
    ycyl(b[0], b[1], -1, panel_t + 1, panel_screw_d);
    ycyl(b[0], b[1], -0.01, (panel_csk_d - panel_screw_d) / 2, panel_csk_d, panel_screw_d);
}

hex_pitch = hex_flat + hex_web;
module grille2d(style = grille_style) { if (style == "sunburst") sunburst2d(); else hex2d(); }
module sunburst2d(d = spk_grille_d, rays = sun_rays, rib_w = sun_rib_w) {
    k = d / spk_grille_d;
    step = 2 * sun_spread / (rays - 1);
    module ray(a, r0) rotate(90 + a) translate([r0, -rib_w / 2]) square([d * 2, rib_w]);
    translate([0, -d / 2]) difference() {
        translate([0, d / 2]) circle(d = d);
        circle(r = sun_hub_r * k);
        for (i = [0 : rays - 1]) ray(-sun_spread + i * step, 0);
        for (i = [0 : rays - 2]) ray(-sun_spread + (i + 0.5) * step, sun_arc_r * k);
        difference() { circle(r = sun_arc_r * k + rib_w / 2); circle(r = sun_arc_r * k - rib_w / 2); }
    }
}
module hex2d() intersection() {
    circle(d = spk_grille_d);
    n = ceil(spk_grille_d / hex_pitch);
    for (j = [-n : n]) for (i = [-n : n])
        translate([(i + (j % 2) / 2) * hex_pitch, j * hex_pitch * sqrt(3) / 2])
            rotate(30) circle(d = hex_flat / cos(30), $fn = 6);
}

logo_z = (zs0 + H) / 2;           // the middle of the band in front of the bay
module logo2d() {
    n = len(logo_lines);
    line_h = logo_w / max([for (l = logo_lines) len(l)]) * 1.0;
    total = n * line_h + (n - 1) * logo_gap;
    for (i = [0 : n - 1])
        translate([0, logo_z + total / 2 - line_h / 2 - i * (line_h + logo_gap)])
            resize([logo_w, line_h])
                text(logo_lines[i], size = 10, font = logo_font, halign = "center", valign = "center");
}

module front(logo = front_logo, grille = grille_style) {
    difference() {
        union() {
            panel_blank();
            for (p = spk_pos) translate([p[0], 0, p[1]])
                yext(panel_t - 0.01, panel_t + collar_h) difference() {
                    circle(d = spk_d + spk_fit + 2 * collar_t);
                    circle(d = spk_d + spk_fit);
                }
            for (p = spk_pos) for (c = clamp_pts(p))
                ycyl(c[0], c[1], panel_t - 0.01, panel_t + clamp_boss_h, clamp_boss_d);
        }
        panel_screw_holes();
        for (p = spk_pos) translate([p[0], 0, p[1]]) {
            yext(-1, panel_t + 1) grille2d(grille);
            yext(grille_t, panel_t + 1) circle(d = spk_grille_d);
        }
        for (p = spk_pos) for (c = clamp_pts(p)) ycyl(c[0], c[1], 1.2, panel_t + clamp_boss_h + 1, clamp_pilot);
        if (logo) yext(-1, logo_depth) logo2d();
    }
}

/* ---------- Clamp tab (as version 1) ---------- */
tab_len  = clamp_r - spk_d / 2 + 2;
tab_drop = clamp_boss_h - collar_h;
tab_foot_x = -(clamp_boss_d / 2 + 0.8 + 0.3);
module tab2d() difference() {
    hull() { circle(d = clamp_boss_d); translate([-tab_len + 2.5, 0]) circle(d = 5); }
    hull() for (dx = [-0.8, 0.8]) translate([dx, 0]) circle(d = 2.8);
}
module tab() {
    translate([0, 0, tab_drop]) linear_extrude(tab_t) tab2d();
    linear_extrude(tab_drop + 0.01) intersection() { tab2d(); translate([tab_foot_x - 20, -10]) square([20, 20]); }
}
module tab_print() translate([0, 0, tab_drop + tab_t]) mirror([0, 0, 1]) tab();

/* ---------- Rear panel + electronics tray ---------- */
// Built in place (outside face at y = D). The tray reaches forward over the
// shelf; the Pi lies on it component side up, ports towards the back, with
// the amp above on its standoffs, so the micro-USB lead plugs straight in
// through the panel. The posts and holes are teardrops pointing to the
// panel: they print sideways (the panel prints face down).
module post2d(d) hull() { circle(d = d); translate([0, d / 2 * sqrt(2)]) square(0.02, center = true); }
module rear() {
    difference() {
        union() {
            translate([0, D, 0]) mirror([0, 1, 0]) panel_blank();
            // the tray
            translate([-inner_w / 2 + lip_clr + lip_t + 0.5, to - tray_len, zt0])
                cube([inner_w - 2 * (lip_clr + lip_t + 0.5), tray_len + 0.01, tray_t]);
            // posts under the Pi
            for (h = pi_holes) translate([h[0], h[1], zt1 - 0.01]) linear_extrude(post_h + 0.01) post2d(post_d);
            // a low rim round the RTC
            translate([0, 0, zt1 - 0.01]) linear_extrude(rtc_rim + 0.01) translate(rtc_pos) difference() {
                square([rtc_w + 2.4, rtc_l + 2.4], center = true);
                square([rtc_w, rtc_l], center = true);
                square([rtc_w + 4, rtc_l - 8], center = true);      // gaps for its header wires
            }
        }
        translate([0, D, 0]) mirror([0, 1, 0]) panel_screw_holes();
        // M2.5 x 10 from under the tray, heads in counterbores
        for (h = pi_holes) {
            translate([h[0], h[1], zt0 - 1]) linear_extrude(tray_t + post_h + 2) post2d(screw_d);
            translate([h[0], h[1], zt0 - 1]) linear_extrude(screw_cbore + 1) post2d(screw_head_d);
        }
        // the power plug, straight in through the panel
        yext(to - 1, D + 1) translate(plug_pos)
            rrect(-plug_w / 2, -plug_h / 2, plug_w / 2, plug_h / 2, 2.5);
    }
}

/* ---------- Knob (as version 1) ---------- */
module knob(style = knob_style) {
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
        translate([0, 0, -0.01]) difference() { cylinder(d = knob_d - 6, h = 4); cylinder(d = shaft_d + 4, h = 4); }
        if (style == "sunburst")
            translate([0, 0, knob_h - knob_engrave]) linear_extrude(knob_engrave + 0.01)
                rotate(-90) sunburst2d(knob_sun_d, knob_sun_rays, knob_sun_rib_w);
        else
            translate([0, -0.7, knob_h - 1]) cube([knob_d / 2 - 3, 1.4, 1.2]);
    }
}

/* ---------- Models, for the checks and pictures ---------- */
module speaker_model(p) color("dimgray") translate([p[0], 0, p[1]]) {
    ycyl(0, 0, ti, ti + spk_rim_t, spk_d);
    ycyl(0, 0, ti + spk_rim_t, ti + spk_depth - 8, spk_d - 6, 20);
    ycyl(0, 0, ti + spk_depth - 8, ti + spk_depth, 24);
}
module tabs_in_place() color("orange") for (p = spk_pos) for (a = clamp_angles)
    translate([p[0], 0, p[1]]) rotate([0, -a, 0]) translate([clamp_r, ti + spk_rim_t, 0]) rotate([-90, 0, 0]) tab();
module pi_stack_model() {
    module board(z, c) color(c) translate([pi_x0, pi_y0, z]) cube([pcb_l, pcb_w, pcb_t]);
    board(pi_z, "green");
    board(amp_z, "darkgreen");
    color("black") translate([pi_x0 + 7, pi_y0 + 0.5, pi_top]) cube([51, 5, amp_gap]);       // GPIO header + socket
    color("black") translate([pi_x0 + 7, pi_y0 + 0.5, pi_z - 2]) cube([51, 5, 2]);           // solder stubs
    color("silver") for (u = [port_otg_u, port_pwr_u]) translate([pi_x0 + u - 4, pi_y1 - 5, pi_top]) cube([8, 5.5, 2.6]);
    color("dimgray") translate([pi_x0 + term_u0, pi_y1 - 7.5, amp_top]) cube([term_u1 - term_u0, 7.9, term_h]);
    color("gold") for (h = pi_holes) translate([h[0], h[1], pi_top]) cylinder(d = 4.5, h = amp_gap);
    color("white", 0.8) translate([plug_pos[0] - 5.5, pi_y1 - 1, pi_top - 1]) cube([11, D - pi_y1 + 20, 8]);   // the plug + lead
    color("steelblue") translate([rtc_pos[0] - rtc_w / 2 + 0.2, rtc_pos[1] - rtc_l / 2 + 0.2, zt1]) cube([rtc_w - 0.4, rtc_l - 0.4, 9]);
}
module encoder_model() color("gray") translate([0, enc_y, 0]) {
    translate([-6.5, -6.5, H - wall - 7]) cube([13, 13, 7]);
    translate([-5, -4, H - wall - 12]) cube([10, 8, 5]);
    translate([0, 0, H - wall - 7]) cylinder(d = 6, h = wall + 7 + 14);
}
module buttons_model() color("goldenrod") for (x = btn_x) translate([x, btn_y, 0]) {
    translate([0, 0, H]) cylinder(d = btn_head_d, h = btn_head_h);
    translate([0, 0, H - wall]) cylinder(d = 11.9, h = wall);
    translate([0, 0, H - wall - 2]) cylinder(d = btn_nut_d, h = 2, $fn = 6);
    translate([0, 0, H - btn_body_h]) cylinder(d = btn_body_d, h = btn_body_h - wall - 2);
}
module knob_placed(style = knob_style) translate([0, enc_y, H + 2]) knob(style);
module speakers_placed() for (p = spk_pos) translate([p[0], 0, p[1]]) {
    ycyl(0, 0, ti, ti + spk_rim_t, spk_d);
    ycyl(0, 0, ti + spk_rim_t, ti + spk_depth - 8, spk_d - 6, 20);
}
module models() { for (p = spk_pos) speaker_model(p); pi_stack_model(); encoder_model(); buttons_model(); }

/* ---------- Print orientation ---------- */
module tube_print()  rotate([90, 0, 0]) translate([0, -ti, 0]) tube();          // front end down
module front_print() rotate([90, 0, 0]) front();                               // face down
module rear_print()  translate([0, 0, D]) rotate([-90, 0, 0]) rear();          // outside face down
module knob_print()  translate([0, 0, knob_h]) rotate([180, 0, 0]) knob();
module tabs_print()  for (i = [0 : 6]) translate([i * (clamp_boss_d + 3), 0, 0]) rotate(90) tab_print();

if (part == "tube") tube_print();
else if (part == "front") front_print();
else if (part == "rear") rear_print();
else if (part == "knob") knob_print();
else if (part == "tabs") tabs_print();
else if (part == "front_plate") {
    front_print();
    translate([-W / 2 + knob_d / 2, knob_d / 2 + 6, 0]) knob_print();
    translate([-W / 2 + knob_d + 10, 12, 0]) tabs_print();
}
else if (part == "assembly") {
    color("burlywood") tube();
    color("tan") front();
    color("tan") rear();
    models();
    tabs_in_place();
    color("orange") knob_placed();
}
else if (part == "exploded") {
    color("burlywood", 0.9) tube();
    translate([0, -60, 0]) { color("tan") front(); for (p = spk_pos) speaker_model(p); tabs_in_place(); }
    translate([0, 90, 0]) { color("tan") rear(); pi_stack_model(); }
    encoder_model(); buttons_model();
    color("orange") knob_placed();
}
// Interference checks: each should come out empty (touching faces show up as
// zero-volume slivers, which is fine).
else if (part == "check") {
    intersection() { tube(); union() { front(); rear(); } }
    intersection() { union() { tube(); front(); rear(); } union() { for (p = spk_pos) speaker_model(p); encoder_model(); buttons_model(); } }
    intersection() { union() { tube(); front(); } pi_stack_model(); }
    intersection() { union() { for (p = spk_pos) speaker_model(p); } union() { pi_stack_model(); encoder_model(); buttons_model(); } }
    intersection() { pi_stack_model(); union() { encoder_model(); buttons_model(); } }
    intersection() { tabs_in_place(); union() { tube(); front(); for (p = spk_pos) speaker_model(p); } }
}
// The back panel with the tray and everything on it slides straight out.
else if (part == "check_pull")
    intersection() {
        hull() for (dy = [0, tube_len + 20]) translate([0, dy, 0]) pi_stack_model();
        union() { tube(); encoder_model(); buttons_model(); }
    }
