#!/bin/bash
# Write the built image to a card in the PC's reader.
#
#   sudo scripts/flash.sh /dev/mmcblk0           # update boot + root only
#   sudo scripts/flash.sh /dev/mmcblk0 --full    # whole card: wipes data + media
#
# On a card that's already a SleepRadioPi-OS card (has the 'data' partition),
# only p1 (boot) and p2 (rootA) are rewritten, so settings on /data and the
# library on /media are kept. Otherwise, or with --full, the whole sdcard.img
# is written; run scripts/provision-media.sh afterwards to add the media.
# Every write is read back and compared.
set -euo pipefail

IMAGES="$(cd "$(dirname "$0")/.." && pwd)/output/images"
DEV=${1:-}; FULL=${2:-}
[ -n "$DEV" ] || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
part() { case "$DEV" in *[0-9]) echo "${DEV}p$1" ;; *) echo "${DEV}$1" ;; esac; }

[ -b "$DEV" ] || { echo "$DEV is not a block device" >&2; exit 1; }
case "$(lsblk -dno TYPE "$DEV")" in disk|loop) ;; *) echo "$DEV is not a whole disk" >&2; exit 1 ;; esac
if lsblk -no MOUNTPOINTS "$DEV" | grep -q .; then
	echo "$DEV has mounted partitions; unmount them first" >&2; exit 1
fi
# Refuse anything bigger than an SD card (a system disk, say).
SIZE_GB=$(( $(lsblk -dbno SIZE "$DEV") / 1000000000 ))
[ "$SIZE_GB" -le 256 ] || { echo "$DEV is ${SIZE_GB} GB: not an SD card?" >&2; exit 1; }

write() {  # write <image> <target>
	echo "writing $(basename "$1") -> $2"
	dd if="$1" of="$2" bs=4M conv=fsync status=none
	cmp -n "$(stat -c %s "$1")" "$1" "$2"
}

lsblk -o NAME,SIZE,LABEL,FSTYPE "$DEV"
echo
if [ "$FULL" != --full ] && [ "$(lsblk -no LABEL "$(part 5)" 2>/dev/null)" = data ]; then
	read -r -p "Update boot + root on $DEV (keeps /data and /media)? [y/N] " ok
	[ "$ok" = y ] || exit 1
	write "$IMAGES/boot.vfat" "$(part 1)"
	write "$IMAGES/rootfs.squashfs" "$(part 2)"
else
	read -r -p "ERASE ALL of $DEV and write sdcard.img? [y/N] " ok
	[ "$ok" = y ] || exit 1
	write "$IMAGES/sdcard.img" "$DEV"
fi
sync
echo "done: verified"
