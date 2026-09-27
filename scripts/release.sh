#!/bin/bash
# Build a release and publish it on GitHub, where radios can update from it
# (Settings -> Updates on the web page).
#
#   scripts/release.sh VERSION [--notes "what's new"] [--sdcard] [--local]
#
#   VERSION     e.g. 1.2.0 (the tag is v1.2.0); shown on the radio's page
#   --notes     a line for the page and the release ("Faster start-up.")
#   --sdcard    also attach sdcard.img.xz, for flashing a new card
#   --local     build and gather the files in output/release/VERSION, but
#               don't tag or publish (serve that folder to test an update:
#               set "update_source" in the radio's config to its manifest.json)
#
# A release has rootfs.squashfs (written into the radio's spare root slot),
# config.txt and manifest.json (version, notes, sizes and SHA-256s, and the
# kernel's SHA-256: a radio refuses an update that changes the kernel).
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
VERSION=${1:-}; shift || true
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,2}(-[a-z0-9.]+)?$ ]] || usage
NOTES="" SDCARD="" LOCAL=""
while [ $# -gt 0 ]; do
	case "$1" in
		--notes) NOTES=$2; shift 2 ;;
		--sdcard) SDCARD=1; shift ;;
		--local) LOCAL=1; shift ;;
		*) usage ;;
	esac
done
TAG="v$VERSION"

if [ -z "$LOCAL" ]; then
	[ -z "$(git status --porcelain)" ] || { echo "commit your changes first" >&2; exit 1; }
	git fetch -q origin
	[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "push main first (and be on it)" >&2; exit 1; }
	! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "$TAG already exists" >&2; exit 1; }
	command -v gh >/dev/null || { echo "needs the GitHub CLI (gh), logged in" >&2; exit 1; }
fi

echo "building $VERSION ..."
make sleepradiopi-dirclean >/dev/null
SLEEPRADIOPI_RELEASE="$VERSION" make   # (not _VERSION: that is the package's own make variable)
IMAGES=output/images
[ "$(cat output/target/etc/sleepradiopi-version)" = "$VERSION" ] || { echo "the version didn't make it into the image" >&2; exit 1; }

OUT="output/release/$VERSION"
rm -rf "$OUT" && mkdir -p "$OUT"
cp "$IMAGES/rootfs.squashfs" "$OUT/rootfs.squashfs"
cp "$IMAGES/rpi-firmware/config.txt" "$OUT/config.txt"
sha() { sha256sum "$1" | cut -c1-64; }
python3 - "$OUT" "$VERSION" "$NOTES" "$(sha "$OUT/rootfs.squashfs")" "$(stat -c %s "$OUT/rootfs.squashfs")" \
	"$(sha "$IMAGES/Image")" "$(sha "$OUT/config.txt")" "$(git rev-parse --short HEAD)" <<'PY'
import json, sys, time
out, version, notes, rsha, rsize, ksha, csha, commit = sys.argv[1:]
manifest = {"version": version, "notes": notes, "commit": commit,
            "built": time.strftime("%Y-%m-%d"),
            "rootfs": {"file": "rootfs.squashfs", "size": int(rsize), "sha256": rsha},
            "kernel_sha256": ksha,
            "config_txt": {"file": "config.txt", "sha256": csha}}
open(f"{out}/manifest.json", "w").write(json.dumps(manifest, indent=2) + "\n")
PY
if [ -n "$SDCARD" ]; then
	echo "compressing the card image ..."
	xz -T0 -6 -c "$IMAGES/sdcard.img" > "$OUT/sdcard.img.xz"
fi
echo "release files in $OUT:"; ls -la "$OUT"

if [ -n "$LOCAL" ]; then
	echo "local only: nothing tagged or published."
	echo "to test an update: (cd $OUT && python3 -m http.server 8765), and on the radio set"
	echo "  \"update_source\": \"http://<this PC>:8765/manifest.json\" in its config.json"
	exit 0
fi

git tag -a "$TAG" -m "SleepRadioPi-OS $VERSION${NOTES:+: $NOTES}"
git push -q origin "$TAG"
gh release create "$TAG" "$OUT"/* --title "SleepRadioPi-OS $VERSION" \
	--notes "${NOTES:-SleepRadioPi-OS $VERSION}

Update a radio from its web page: Settings -> Updates. For a new card, flash sdcard.img.xz (if attached) or build it yourself (see the README)."
echo "published $TAG"
