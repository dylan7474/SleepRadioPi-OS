#!/bin/bash
# Fill the media partition of a SleepRadioPi-OS card, on the PC.
#
#   sudo scripts/provision-media.sh /dev/mmcblk0 \
#       --music ~/Music/SleepRadioMusic --voices ~/voices \
#       [--jingles ~/Music/SleepRadioJingles] [--hooks dj_hooks_70s.txt]
#
# The card must already hold output/images/sdcard.img. The first run adds p6
# (the media partition) in the free space after the image, filling the card,
# and formats it. Later runs keep it and just sync the files (rsync --delete
# per folder), so a library change only copies what changed. --delete only
# removes files ON THE CARD that are gone from the source; the PC is never
# written to.
#
# A card reader is ~10x faster than copying over the Zero's Wi-Fi. Also
# writes a starter config to /data (only if there isn't one) that points
# the station at /media.
#
# Layout on /media:
#   music/            the library, as-is
#   voices/<id>/      voice packs (model.onnx, tokens.txt, espeak-ng-data/)
#   jingles/          station jingles (mp3 only; the app skips the rest)
#   dj_hooks_70s.txt  optional
set -euo pipefail

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

DEV=${1:-}; [ -n "$DEV" ] || usage; shift
MUSIC= VOICES= JINGLES= HOOKS=
while [ $# -gt 0 ]; do
	case "$1" in
		--music)   MUSIC=$2; shift 2 ;;
		--voices)  VOICES=$2; shift 2 ;;
		--jingles) JINGLES=$2; shift 2 ;;
		--hooks)   HOOKS=$2; shift 2 ;;
		*) usage ;;
	esac
done
[ -n "$MUSIC" ] && [ -n "$VOICES" ] || usage
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

# Partition device names: mmcblk0 -> mmcblk0p1, sdb -> sdb1.
part() { case "$DEV" in *[0-9]) echo "${DEV}p$1" ;; *) echo "${DEV}$1" ;; esac; }

# --- Refuse anything that isn't an unmounted SleepRadioPi-OS card ---------
[ -b "$DEV" ] || { echo "$DEV is not a block device" >&2; exit 1; }
case "$(lsblk -dno TYPE "$DEV")" in disk|loop) ;; *) echo "$DEV is not a whole disk" >&2; exit 1 ;; esac
if lsblk -no MOUNTPOINTS "$DEV" | grep -q .; then
	echo "$DEV has mounted partitions; unmount them first" >&2; exit 1
fi
if [ "$(lsblk -no LABEL "$(part 5)" 2>/dev/null)" != data ] ||
   [ "$(lsblk -no FSTYPE "$(part 1)" 2>/dev/null)" != vfat ]; then
	echo "$DEV doesn't look like a SleepRadioPi-OS card (no FAT p1 + 'data' p5)" >&2
	exit 1
fi
for d in "$MUSIC" "$VOICES" ${JINGLES:+"$JINGLES"}; do
	[ -d "$d" ] || { echo "no such folder: $d" >&2; exit 1; }
done
[ -z "$HOOKS" ] || [ -f "$HOOKS" ] || { echo "no such file: $HOOKS" >&2; exit 1; }
for v in "$VOICES"/*/; do
	[ -f "$v/model.onnx" ] && [ -f "$v/tokens.txt" ] ||
		{ echo "not a voice pack: $v (needs model.onnx + tokens.txt)" >&2; exit 1; }
done

MEDIA=$(part 6)

# --- First run: add p6 filling the card, and format it --------------------
if [ ! -b "$MEDIA" ]; then
	lsblk -o NAME,SIZE,LABEL "$DEV"
	echo
	read -r -p "Add a media partition filling the rest of $DEV? [y/N] " ok
	[ "$ok" = y ] || exit 1
	# Grow the extended partition (p4) to the end of the card, then add
	# logical p6 in the free space after p5.
	echo ',+' | sfdisk --no-reread -q -N 4 "$DEV"
	echo ',+,L' | sfdisk --no-reread -q -N 6 "$DEV"
	partprobe "$DEV"; udevadm settle
	[ -b "$MEDIA" ] || { echo "p6 didn't appear" >&2; exit 1; }
	# One inode per 64 KB is plenty for music; no root-reserved blocks.
	mkfs.ext4 -q -L media -m 0 -i 65536 "$MEDIA"
fi
[ "$(lsblk -no LABEL "$MEDIA")" = media ] ||
	{ echo "$MEDIA isn't labelled 'media'; not touching it" >&2; exit 1; }

# --- Copy -------------------------------------------------------------------
MNT=$(mktemp -d)
DMNT=$(mktemp -d)
cleanup() { umount "$MNT" "$DMNT" 2>/dev/null || true; rmdir "$MNT" "$DMNT"; }
trap cleanup EXIT
mount "$MEDIA" "$MNT"

# Root-owned and world-readable: the station only reads these.
RSYNC=(rsync -rt --delete --info=progress2 --no-inc-recursive
	--chown=0:0 --chmod=D755,F644)
echo "music:";  "${RSYNC[@]}" "$MUSIC/"  "$MNT/music/"
echo "voices:"; "${RSYNC[@]}" "$VOICES/" "$MNT/voices/"
if [ -n "$JINGLES" ]; then
	echo "jingles:"; "${RSYNC[@]}" --include='*/' --include='*.mp3' --exclude='*' \
		"$JINGLES/" "$MNT/jingles/"
fi
[ -z "$HOOKS" ] || install -m 644 -o 0 -g 0 "$HOOKS" "$MNT/dj_hooks_70s.txt"
sync

# --- Starter config on /data -------------------------------------------------
mount "$(part 5)" "$DMNT"
CONF="$DMNT/radio/.config/sleepradiopi/config.json"
if [ ! -f "$CONF" ]; then
	mkdir -p "$(dirname "$CONF")"
	cat > "$CONF" <<-EOF
	{
	  "music_folder": "/media/music",
	  "voices_folder": "/media/voices",
	  "jingles_folder": "/media/jingles",
	  "hooks_file": "/media/dj_hooks_70s.txt"
	}
	EOF
	echo "wrote a starter config: /data/radio/.config/sleepradiopi/config.json"
fi
sync

echo
df -h "$MNT" | tail -1
echo "done: $MEDIA (media) is ready; eject the card and boot the Pi"
