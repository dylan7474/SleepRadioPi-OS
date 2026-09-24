#!/bin/bash
# Update a running Pi over ssh: A/B root slots.
#
#   scripts/update.sh [ssh-host]        (default: sleepradiopi-os)
#
# Writes output/images/rootfs.squashfs to the root slot the Pi isn't running
# from (p2 or p3), verifies it, points cmdline.txt at it and reboots, then
# waits for the station to come back. /data and /media aren't touched. The
# old slot is left as it was, so going back is just switching cmdline.txt.
#
# Only the root is updated: the kernel is on the shared boot partition and
# must match the modules in the root, so if the kernel changed this refuses
# and you use scripts/flash.sh with the card in the PC instead.
#
# If the Pi doesn't come back: card in the PC, and on the boot partition
# change root=/dev/mmcblk0pN in cmdline.txt back to the old slot (or run
# scripts/flash.sh).
set -euo pipefail

HOST=${1:-sleepradiopi-os}
IMAGES="$(cd "$(dirname "$0")/.." && pwd)/output/images"
ROOT="$IMAGES/rootfs.squashfs"
SLOT_SIZE=$((256 * 1024 * 1024))
pi() { ssh -o ConnectTimeout=5 -o BatchMode=yes "$HOST" "$@"; }

[ -f "$ROOT" ] || { echo "no $ROOT: run make first" >&2; exit 1; }
SIZE=$(stat -c %s "$ROOT")
[ "$SIZE" -le "$SLOT_SIZE" ] || { echo "rootfs.squashfs is bigger than a 256 MB slot" >&2; exit 1; }

CUR=$(pi 'grep -o "root=/dev/mmcblk0p[23]" /proc/cmdline' | sed 's/root=//') ||
	{ echo "can't reach $HOST, or it isn't booted from p2/p3" >&2; exit 1; }
case "$CUR" in
	/dev/mmcblk0p2) NEW=/dev/mmcblk0p3 ;;
	/dev/mmcblk0p3) NEW=/dev/mmcblk0p2 ;;
esac
echo "running from $CUR; updating $NEW"

if [ "$(sha256sum < "$IMAGES/Image" | cut -c1-64)" != "$(pi 'sha256sum < /boot/Image' | cut -c1-64)" ]; then
	echo "the kernel changed: update with the card in the PC (scripts/flash.sh)" >&2
	exit 1
fi
if pi "grep -q '^$NEW ' /proc/mounts"; then
	echo "$NEW is mounted on the Pi; not writing to it" >&2; exit 1
fi

echo "writing $(( SIZE / 1000000 )) MB ..."
pi "dd of=$NEW bs=1M 2>/dev/null && sync" < "$ROOT"
WANT=$(sha256sum < "$ROOT" | cut -c1-64)
GOT=$(pi "head -c $SIZE $NEW | sha256sum" | cut -c1-64)
[ "$WANT" = "$GOT" ] || { echo "verify FAILED: $NEW doesn't match; still booting $CUR" >&2; exit 1; }
echo "verified"

# The one write to the boot partition: a single small file, synced at once.
pi "mount -o remount,rw /boot &&
	sed -i 's|root=$CUR|root=$NEW|' /boot/cmdline.txt && sync &&
	mount -o remount,ro /boot && grep -q 'root=$NEW' /boot/cmdline.txt"
echo "cmdline.txt now boots $NEW; rebooting"
pi reboot || true

sleep 10
for _ in $(seq 60); do
	pi true 2>/dev/null && break
	sleep 3
done
BOOTED=$(pi 'grep -o "root=/dev/mmcblk0p[23]" /proc/cmdline' 2>/dev/null | sed 's/root=//') || true
if [ "$BOOTED" != "$NEW" ]; then
	echo "the Pi didn't come back on $NEW (got: '${BOOTED:-no answer}')." >&2
	echo "Card in the PC; in cmdline.txt on the boot partition set root=$CUR." >&2
	exit 1
fi
echo "booted $NEW; waiting for the station ..."
for _ in $(seq 60); do
	pi 'wget -q -T 5 -O /dev/null http://127.0.0.1/api/status' 2>/dev/null &&
		{ echo "done: the station is up on $NEW"; exit 0; }
	sleep 5
done
echo "booted $NEW but the station hasn't answered after 5 minutes:" >&2
echo "  ssh $HOST 'grep sleepradiopi /var/log/messages | tail'" >&2
exit 1
