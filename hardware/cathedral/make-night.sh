#!/bin/sh
# A night-time picture of the wood cathedral radio with its lamps on: the
# grille cloth lit from behind (the fretwork in silhouette) and the VU
# meter's warm backlight, glowing onto the wood. -> concept_night.jpg
#
# OpenSCAD has no lights, so: render the wood radio, render it again with
# only the lit parts white (a mask), then darken, light and bloom it with
# ImageMagick. Needs openscad + ImageMagick.
set -e
cd "$(dirname "$0")"
cam=${CAM:-0,0,175,84,0,9,1250}
S=2400
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
scad() { openscad --backend=manifold --render --camera=$cam --imgsize=$S,$S "$@" concept.scad 2>/dev/null; }
wood() { scad "$@" -D 'cabinet_colour="#4e2f1c"' -D 'moulding_colour="#4e2f1c"' -D 'face_colour="#9a6a3f"' \
	-D 'plaque_colour="#b8913f"' -D 'letter_colour="#2b1a10"' -D 'knob_colour="#3a2416"' -D 'cloth_colour="#c2a36e"'; }

wood --colorscheme=Tomorrow -o "$tmp/radio.png"
wood --colorscheme=Sunset -o "$tmp/key.png"                            # to cut the radio out
# only the lit parts white: the grille cloth and the meter face
scad --colorscheme=Sunset -D 'cabinet_colour="#000000"' -D 'moulding_colour="#000000"' -D 'face_colour="#000000"' \
	-D 'plaque_colour="#000000"' -D 'letter_colour="#000000"' -D 'knob_colour="#000000"' \
	-D 'cloth_colour="#ffffff"' -D 'meter_colour="#ffffff"' -o "$tmp/lit.png"

magick "$tmp/key.png" -fuzz 1% -fill black -opaque 'srgb(170,68,68)' -fill white +opaque black "$tmp/mask.png"
# the lit parts: near-white, not the red background, not the black radio
magick "$tmp/lit.png" -fuzz 1% -fill black -opaque 'srgb(170,68,68)' -colorspace gray -level 55%,75% "$tmp/litmask.png"

# where the lamp is: the middle of the grille cloth, low down (behind the hub)
bbox=$(magick "$tmp/litmask.png" -threshold 50% -format '%@' info:)   # WxH+X+Y of everything lit
W=${bbox%%x*}; rest=${bbox#*x}; H=${rest%%+*}; rest=${rest#*+}; X=${rest%%+*}; Y=${rest#*+}
cx=$((X + W / 2)); cy=$((Y + H * 45 / 100))
# the whole radio, for the pool of light under it
rb=$(magick "$tmp/mask.png" -threshold 50% -format '%@' info:)
RW=${rb%%x*}; r=${rb#*x}; RH=${r%%+*}; r=${r#*+}; RX=${r%%+*}; RY=${r#*+}

# night: the radio darkened and cooled
magick "$tmp/radio.png" -modulate 38,80 -fill '#0d1330' -colorize 22% "$tmp/dark.png"
# lamp light: the lit parts warm and bright, strongest near the lamp
magick -size ${S}x${S} radial-gradient:white-'#3a2008' -distort SRT "$((S/2)),$((S/2)) 0.95 0 $cx,$cy" "$tmp/falloff.png"
magick "$tmp/radio.png" -modulate 118,140 -fill '#ff9a30' -colorize 38% "$tmp/falloff.png" -compose Multiply -composite "$tmp/warm.png"
magick "$tmp/dark.png" "$tmp/warm.png" "$tmp/litmask.png" -compose Over -composite "$tmp/lit_radio.png"
# bloom: the light spilling onto the wood round the grille and the dial
magick "$tmp/litmask.png" -blur 0x60 +level 0,70% -fill '#ff9d3a' -colorize 100 "$tmp/litmask.png" -blur 0x60 -compose CopyOpacity -composite "$tmp/glow.png"
magick "$tmp/lit_radio.png" "$tmp/glow.png" -compose Screen -composite "$tmp/radio_night.png"
magick "$tmp/radio_night.png" "$tmp/mask.png" -alpha off -compose CopyOpacity -composite "$tmp/cut.png"

# a dark room, and a warm pool of light on the table in front
magick -size ${S}x${S} radial-gradient:'#1c2033'-'#05060b' "$tmp/bg.png"
magick -size ${S}x${S} xc:none -fill '#ff9d3a' -draw "ellipse $((RX + RW / 2)),$((RY + RH - RH / 40)) $((RW * 7 / 10)),$((RH / 14)) 0,360" -blur 0x70 -channel A -evaluate multiply 0.3 +channel "$tmp/pool.png"
magick "$tmp/bg.png" "$tmp/pool.png" -compose Screen -composite "$tmp/cut.png" -compose Over -composite \
	-resize 1200x1200 -quality 92 concept_night.jpg
echo "wrote concept_night.jpg"
