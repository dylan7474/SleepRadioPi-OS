"""Draw wiring.svg: how the SleepRadioPi's parts connect to the Pi's header.

    python3 hardware/wiring/make_wiring.py      (writes hardware/wiring/wiring.svg)

The pins match board/sleepradiopi/config.txt: the MiniAmp's I2S (hifiberry-dac),
the rotary encoder on GPIO17/27 with its switch on GPIO22, the DS3231 on
I2C (GPIO2/3), and the four preset buttons on GPIO5/6/16/26. Keep the two in step.
"""

from pathlib import Path

W, H = 1160, 1030
ROW = 24                      # header pin pitch in the drawing
HX, HY = 560, 150             # header: left column centre x, pin 1 centre y
COL = 180                     # distance between the two columns (pin names go between them)

# Every header pin: (label, kind). kind picks the colour of the pin dot.
PINS = {
    1: ("3.3V", "p3"), 2: ("5V", "p5"), 3: ("GPIO2 SDA", "io"), 4: ("5V", "p5"),
    5: ("GPIO3 SCL", "io"), 6: ("GND", "g"), 7: ("GPIO4", "io"), 8: ("GPIO14", "io"),
    9: ("GND", "g"), 10: ("GPIO15", "io"), 11: ("GPIO17", "io"), 12: ("GPIO18", "io"),
    13: ("GPIO27", "io"), 14: ("GND", "g"), 15: ("GPIO22", "io"), 16: ("GPIO23", "io"),
    17: ("3.3V", "p3"), 18: ("GPIO24", "io"), 19: ("GPIO10", "io"), 20: ("GND", "g"),
    21: ("GPIO9", "io"), 22: ("GPIO25", "io"), 23: ("GPIO11", "io"), 24: ("GPIO8", "io"),
    25: ("GND", "g"), 26: ("GPIO7", "io"), 27: ("ID_SD", "io"), 28: ("ID_SC", "io"),
    29: ("GPIO5", "io"), 30: ("GND", "g"), 31: ("GPIO6", "io"), 32: ("GPIO12", "io"),
    33: ("GPIO13", "io"), 34: ("GND", "g"), 35: ("GPIO19", "io"), 36: ("GPIO16", "io"),
    37: ("GPIO26", "io"), 38: ("GPIO20", "io"), 39: ("GND", "g"), 40: ("GPIO21", "io"),
}
DOT = {"p3": "#e8a33d", "p5": "#d9453b", "g": "#222", "io": "#9aa0a6"}

# Wire colours
RED, ORANGE, BLACK = "#d9453b", "#e8a33d", "#222"
BLUE, YELLOW, GREEN, TEAL, PURPLE, PINK = "#2f6fd6", "#d4b000", "#2e9e4f", "#1a9c9c", "#8a4fd6", "#d64f9a"


def pin_xy(n: int) -> tuple[float, float]:
    row = (n - 1) // 2
    return (HX if n % 2 else HX + COL), HY + row * ROW


out = []
add = out.append
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    'font-family="DejaVu Sans, Liberation Sans, Arial, sans-serif">')
add(f'<rect width="{W}" height="{H}" fill="#fbfaf7"/>')
add('<text x="30" y="44" font-size="26" font-weight="bold" fill="#222">SleepRadioPi wiring</text>')
add('<text x="30" y="70" font-size="14" fill="#555">Raspberry Pi Zero 2 W header seen from above, pin 1 '
    'at the top left (next to the SD card end). Only the coloured wires are needed.</text>')


def box(x, y, w, h, title, lines, fill="#fff"):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="#333" stroke-width="1.5"/>')
    add(f'<text x="{x + 12}" y="{y + 24}" font-size="16" font-weight="bold" fill="#222">{title}</text>')
    for i, line in enumerate(lines):
        add(f'<text x="{x + 12}" y="{y + 44 + i * 17}" font-size="12.5" fill="#444">{line}</text>')


def terminal(x, y, label, side):
    """A labelled connection point on a part: a small circle and its name."""
    add(f'<circle cx="{x}" cy="{y}" r="4.5" fill="#fff" stroke="#333" stroke-width="1.5"/>')
    anchor, dx = ("end", -9) if side == "right" else ("start", 9)
    add(f'<text x="{x + dx}" y="{y + 4.5}" font-size="12.5" fill="#222" text-anchor="{anchor}">{label}</text>')


def wire(points, colour, dashed=False):
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    add(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="3.2" stroke-linecap="round" '
        f'stroke-linejoin="round"{dash}/>')


# --- the header ---------------------------------------------------------------------
top, bottom = HY - 18, HY + 19 * ROW + 18
add(f'<rect x="{HX - 18}" y="{top}" width="{COL + 36}" height="{bottom - top}" rx="8" fill="#1f5a3a"/>')
add(f'<text x="{HX + COL / 2}" y="{top - 10}" font-size="13" fill="#1f5a3a" text-anchor="middle" '
    'font-weight="bold">Pi header</text>')

# --- parts --------------------------------------------------------------------------
# Real-time clock (optional), top left
RTC = (60, 105, 250, 150)
box(*RTC, "DS3231 RTC (optional)", ["ZS-042 board. 3.3 V only!", "With a plain CR2032, remove its",
                                     "charging resistor (201) or diode."], "#f1f5ff")
rtc_t = {"VCC": (RTC[0] + RTC[2], 205), "SDA": (RTC[0] + RTC[2], 222),
         "SCL": (RTC[0] + RTC[2], 239), "GND": (RTC[0] + RTC[2], 256 - 1)}
# Rotary encoder, middle left
ENC = (60, 330, 250, 300)
box(*ENC, "Rotary encoder + switch", ["Turn: volume. Press: pause/play.", "Hold 3 s: says the IP address.",
                                      "KY-040 module: pins in this order."], "#f1fff4")
# In the order printed on a KY-040 module
enc_t = {"CLK (A)": (ENC[0] + ENC[2], 430), "DT (B)": (ENC[0] + ENC[2], 448),
         "SW": (ENC[0] + ENC[2], 466), "+ (KY-040 only)": (ENC[0] + ENC[2], 484), "GND": (ENC[0] + ENC[2], 502)}
for i, line in enumerate(["Bare EC11 (no board): on the 3-pin", "side, the middle pin is GND, the outer",
                          "two CLK and DT; of the 2 pins, one to", "SW, the other to GND.",
                          "Volume goes the wrong way? Swap CLK/DT."]):
    add(f'<text x="{ENC[0] + 12}" y="{530 + i * 17}" font-size="12.5" fill="#444">{line}</text>')
# MiniAmp, right
AMP = (850, 150, 270, 300)
box(*AMP, "HiFiBerry MiniAmp", ["Normally plugged straight onto all", "40 pins. Wired by hand instead,",
                               "these 7 wires are all it needs.", "", "Speakers: see below."], "#fff6ee")
amp_t = {"5V": (AMP[0], 282), "5V ": (AMP[0], 300), "GND": (AMP[0], 318), "BCLK (GPIO18)": (AMP[0], 336),
         "DIN (GPIO21)": (AMP[0], 354), "LRCLK (GPIO19)": (AMP[0], 390), "GND ": (AMP[0], 408)}

for name, (x, y) in rtc_t.items():
    terminal(x, y, name, "right")
for name, (x, y) in enc_t.items():
    terminal(x, y, name, "right")
for name, (x, y) in amp_t.items():
    terminal(x, y, name.strip(), "left")

# --- wires --------------------------------------------------------------------------
L = HX - 18               # the header's left edge
R = HX + COL + 18         # and its right edge


def left_wire(term, pin, colour, lane, dashed=False):
    tx, ty = term
    px, py = pin_xy(pin)
    wire([(tx + 5, ty), (lane, ty), (lane, py), (px, py)], colour, dashed)


def right_wire(term, pin, colour, lane):
    tx, ty = term
    px, py = pin_xy(pin)
    wire([(tx - 5, ty), (lane, ty), (lane, py), (px, py)], colour)


left_wire(rtc_t["VCC"], 1, ORANGE, 450)
left_wire(rtc_t["SDA"], 3, BLUE, 466)
left_wire(rtc_t["SCL"], 5, YELLOW, 482)
left_wire(rtc_t["GND"], 9, BLACK, 498)
left_wire(enc_t["CLK (A)"], 11, GREEN, 450)
left_wire(enc_t["DT (B)"], 13, TEAL, 466)
left_wire(enc_t["SW"], 15, PURPLE, 482)
left_wire(enc_t["+ (KY-040 only)"], 17, ORANGE, 498, dashed=True)
left_wire(enc_t["GND"], 25, BLACK, 514)

# The higher the pin, the further out its lane, so the wires nest without crossing
right_wire(amp_t["5V"], 2, RED, 838)
right_wire(amp_t["5V "], 4, RED, 826)
right_wire(amp_t["GND"], 6, BLACK, 814)
right_wire(amp_t["BCLK (GPIO18)"], 12, PINK, 802)
right_wire(amp_t["DIN (GPIO21)"], 40, BLUE, 790)
# The two odd-side pins go round the bottom of the header
for term, pin, colour, dip in ((amp_t["LRCLK (GPIO19)"], 35, YELLOW, 18), (amp_t["GND "], 39, BLACK, 30)):
    tx, ty = term
    px, py = pin_xy(pin)
    low = bottom + dip
    lane_r = 776 + dip / 2
    lane_l = HX - 30 - dip / 2
    wire([(tx - 5, ty), (lane_r, ty), (lane_r, low), (lane_l, low), (lane_l, py), (px, py)], colour)

# Preset buttons: gold rings on their pins (wired as in the box below the header)
BUTTONS = {29: "B1", 31: "B2", 36: "B3", 37: "B4"}
GOLD = "#c99a06"
for n, tag in BUTTONS.items():
    x, y = pin_xy(n)
    add(f'<circle cx="{x}" cy="{y}" r="11" fill="none" stroke="{GOLD}" stroke-width="3"/>')
    tx, anchor = (x - 16, "end") if n % 2 else (x + 16, "start")
    add(f'<text x="{tx}" y="{y + 4}" font-size="11" font-weight="bold" fill="{GOLD}" '
        f'text-anchor="{anchor}">{tag}</text>')

# Header pins (drawn over the wire ends) and their names
for n, (label, kind) in PINS.items():
    x, y = pin_xy(n)
    add(f'<circle cx="{x}" cy="{y}" r="7" fill="{DOT[kind]}" stroke="#fff" stroke-width="1.5"/>')
    add(f'<text x="{x}" y="{y + 3.5}" font-size="8.5" fill="#fff" text-anchor="middle" '
        f'font-weight="bold">{n}</text>')
    colour = "#ffd28a" if kind in ("p3", "p5") else "#cfe9d9" if kind == "io" else "#9fb8aa"
    if n % 2:
        add(f'<text x="{x + 14}" y="{y + 4}" font-size="11" fill="{colour}">{label}</text>')
    else:
        add(f'<text x="{x - 14}" y="{y + 4}" font-size="11" fill="{colour}" text-anchor="end">{label}</text>')

# --- speakers and power -------------------------------------------------------------
SPK_Y = AMP[1] + AMP[3] + 40
for i, label in enumerate(("Left speaker", "Right speaker")):
    cx = AMP[0] + 60 + i * 150
    wire([(cx - 8, AMP[1] + AMP[3]), (cx - 8, SPK_Y)], RED)
    wire([(cx + 8, AMP[1] + AMP[3]), (cx + 8, SPK_Y)], BLACK)
    add(f'<text x="{cx - 8}" y="{AMP[1] + AMP[3] - 8}" font-size="12" fill="#222" text-anchor="middle">+</text>')
    add(f'<text x="{cx + 8}" y="{AMP[1] + AMP[3] - 8}" font-size="12" fill="#222" text-anchor="middle">−</text>')
    add(f'<rect x="{cx - 32}" y="{SPK_Y}" width="64" height="40" rx="7" fill="#333"/>')
    add(f'<circle cx="{cx}" cy="{SPK_Y + 20}" r="14" fill="#666"/>')
    add(f'<text x="{cx}" y="{SPK_Y + 58}" font-size="12.5" fill="#222" text-anchor="middle">{label}</text>')
box(AMP[0], SPK_Y + 76, AMP[2], 112, "Speakers (4–8 Ω)", ["Each to its own channel's + and −",
                                                          "(marked on the MiniAmp's terminal).",
                                                          "Never join the two − together.",
                                                          "One speaker? Set Mono on the page."], "#fff6ee")

box(60, 700, 470, 90, "Power", ["5 V, 2.5 A or more, micro-USB into the Pi's PWR IN", "port (the one at the end of the board). The MiniAmp",
                                "takes its power from the Pi's 5 V pins."], "#fff")

BX, BY = 575, 700
box(BX, BY, 290, 190, "Preset buttons (optional)", [
    "Momentary push buttons. Each: one leg",
    "to its pin, the other to any GND pin.",
    "B1 → pin 29 (GPIO5)",
    "B2 → pin 31 (GPIO6)",
    "B3 → pin 36 (GPIO16)",
    "B4 → pin 37 (GPIO26)",
    "GND: pins 30, 34 or 39 (one wire can",
    "loop from button to button)."], "#fffbea")
for i in range(4):                         # the four buttons, beside the list
    cx, cy = BX + 250, BY + 83 + i * 17
    add(f'<circle cx="{cx}" cy="{cy - 4}" r="7" fill="#333"/>')
    add(f'<circle cx="{cx}" cy="{cy - 4}" r="4" fill="{GOLD}"/>')

# Legend
LY = 945
add(f'<text x="60" y="{LY}" font-size="13" fill="#222" font-weight="bold">Pin colours:</text>')
for (kind, text), x in zip((("p5", "5 V"), ("p3", "3.3 V"), ("g", "Ground (any GND pin will do)"),
                           ("io", "GPIO")), (170, 250, 350, 590)):
    add(f'<circle cx="{x}" cy="{LY - 4}" r="7" fill="{DOT[kind]}"/>')
    add(f'<text x="{x + 12}" y="{LY}" font-size="12.5" fill="#444">{text}</text>')
add(f'<text x="60" y="{LY + 30}" font-size="12.5" fill="#444">Dashed: only for encoder modules with their own '
    'pull-ups (KY-040), to 3.3 V, never 5 V.</text>')
add(f'<text x="60" y="{LY + 48}" font-size="12.5" fill="#444">The knob and RTC pins are under the MiniAmp: solder '
    'their wires to the underside of the Pi\'s header, or fit a stacking header / GPIO extender between them.</text>')
add('</svg>')

path = Path(__file__).with_name("wiring.svg")
path.write_text("\n".join(out) + "\n")
print(f"wrote {path}")
