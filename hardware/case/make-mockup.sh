#!/bin/sh
# Render mockup.scad to mockup.jpg: the radio cut out from a flat render and
# placed on a soft studio backdrop with a shadow. Needs openscad + ImageMagick.
# LOGO=1 shows the SLEEP RADIO front (mockup_logo.jpg); SUNBURST=1 as well
# gives the art deco grilles and knob (mockup_logo_sunburst.jpg); HANDLE=1 adds the
# carry strap (..._handle.jpg).
set -e
cd "$(dirname "$0")"
tmp=$(mktemp -d)
cam=${CAM:-0,40,40,76,0,22,600}
size=3200,2400          # rendered at 2x, scaled down at the end to smooth edges
logo=false; out=mockup.jpg
grille=hex; knob=plain
if [ -n "$LOGO" ]; then logo=true; out=mockup_logo.jpg; fi
if [ -n "$SUNBURST" ]; then grille=sunburst; knob=sunburst; out=${out%.jpg}_sunburst.jpg; fi
handle=false
if [ -n "$HANDLE" ]; then handle=true; out=${out%.jpg}_handle.jpg; cam=${CAM:-0,40,70,76,0,22,700}; fi

openscad --backend=manifold --render --camera=$cam --imgsize=$size --colorscheme=Tomorrow -D mockup_logo=$logo -D "mockup_grille=\"$grille\"" -D "mockup_knob=\"$knob\"" -D mockup_handle=$handle -o "$tmp/radio.png" mockup.scad 2>/dev/null
# Same view on a flat red background, used only to cut the radio out
openscad --backend=manifold --render --camera=$cam --imgsize=$size --colorscheme=Sunset -D mockup_logo=$logo -D "mockup_grille=\"$grille\"" -D "mockup_knob=\"$knob\"" -D mockup_handle=$handle -o "$tmp/key.png" mockup.scad 2>/dev/null

magick "$tmp/key.png" -fuzz 1% -fill black -opaque 'srgb(170,68,68)' -fill white +opaque black "$tmp/mask.png"
magick "$tmp/radio.png" "$tmp/mask.png" -alpha off -compose CopyOpacity -composite "$tmp/cut.png"

# Backdrop: warm grey gradient with a vignette
magick -size 3200x2400 radial-gradient:'#f4f1ec'-'#b9b4ac' "$tmp/bg.png"
# Soft contact shadow under the radio
magick "$tmp/mask.png" -fill black -colorize 100 -alpha copy \
    -channel A -evaluate multiply 0.45 +channel -blur 0x40 "$tmp/shadow.png"

magick "$tmp/bg.png" \
    "$tmp/shadow.png" -geometry +30+60 -composite \
    "$tmp/cut.png" -composite \
    -resize 1600x1200 -quality 92 "$out"
rm -rf "$tmp"
echo "wrote $out"
