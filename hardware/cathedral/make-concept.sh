#!/bin/sh
# Render concept.scad to concept.jpg (three-quarter view) and
# concept_front.jpg: the radio cut out from a flat render and placed on a soft
# studio backdrop with a shadow, as the box's make-mockup.sh does.
# Needs openscad + ImageMagick.
set -e
cd "$(dirname "$0")"
render() {   # camera, output
	tmp=$(mktemp -d)
	size=2400,2400          # rendered at 2x, scaled down at the end to smooth edges
	openscad --backend=manifold --render --camera=$1 --imgsize=$size --colorscheme=Tomorrow -o "$tmp/radio.png" concept.scad 2>/dev/null
	openscad --backend=manifold --render --camera=$1 --imgsize=$size --colorscheme=Sunset -o "$tmp/key.png" concept.scad 2>/dev/null
	magick "$tmp/key.png" -fuzz 1% -fill black -opaque 'srgb(170,68,68)' -fill white +opaque black "$tmp/mask.png"
	magick "$tmp/radio.png" "$tmp/mask.png" -alpha off -compose CopyOpacity -composite "$tmp/cut.png"
	magick -size 2400x2400 radial-gradient:'#f4f1ec'-'#b9b4ac' "$tmp/bg.png"
	magick "$tmp/mask.png" -fill black -colorize 100 -alpha copy \
		-channel A -evaluate multiply 0.45 +channel -blur 0x40 "$tmp/shadow.png"
	magick "$tmp/bg.png" "$tmp/shadow.png" -geometry +30+60 -composite "$tmp/cut.png" -composite \
		-resize 1200x1200 -quality 92 "$2"
	rm -rf "$tmp"
	echo "wrote $2"
}
render ${CAM:-0,100,175,80,0,22,1350} concept.jpg
render ${CAM_FRONT:-0,0,175,84,0,9,1250} concept_front.jpg
