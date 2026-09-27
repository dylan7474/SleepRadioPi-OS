#!/bin/sh
# Runs after the target tree is assembled, before the rootfs image is made.
set -eu

LOCAL="${BR2_EXTERNAL_SLEEPRADIOPI_PATH}/local"

# SSH host keys: generate once per build machine and keep them in local/
# (git-ignored), so the Pi keeps the same identity across rebuilds and the
# read-only root never has to write them.
mkdir -p "${LOCAL}/ssh"
for t in ed25519 rsa; do
	k="${LOCAL}/ssh/ssh_host_${t}_key"
	[ -f "$k" ] || ssh-keygen -q -t "$t" -N '' -C sleepradiopi -f "$k"
	install -m 600 "$k" "${TARGET_DIR}/etc/ssh/"
	install -m 644 "$k.pub" "${TARGET_DIR}/etc/ssh/"
done

mkdir -p "${TARGET_DIR}/boot"

# The image's version, shown on the web page and compared with GitHub
# releases: scripts/release.sh sets SLEEPRADIOPI_RELEASE; otherwise it's
# git's description of this checkout (e.g. v1.0.0-3-gabc1234-dirty).
echo "${SLEEPRADIOPI_RELEASE:-$(git -C "${BR2_EXTERNAL_SLEEPRADIOPI_PATH}" describe --tags --always --dirty 2>/dev/null || echo unknown)}" \
	> "${TARGET_DIR}/etc/sleepradiopi-version"

# When this image was built: S12rtc won't believe an RTC that's earlier.
date +%s > "${TARGET_DIR}/etc/build-time"

# No console on the HDMI port: it's headless, and it saves a getty.
sed -i '/^tty1::/d' "${TARGET_DIR}/etc/inittab"

# Mount points for the data (rw) and media (ro) partitions; see S00data.
mkdir -p "${TARGET_DIR}/data" "${TARGET_DIR}/media"

# Keep the random seed on /data so it survives reboots (seedrng credits it
# only if it could be saved; a read-only /var/lib would mean a fresh seed
# each boot).
rm -rf "${TARGET_DIR}/var/lib/seedrng"
ln -sfn /data/seedrng "${TARGET_DIR}/var/lib/seedrng"

# The station: init starts it after rcS and restarts it if it exits.
grep -q sleepradiopi-station "${TARGET_DIR}/etc/inittab" ||
	echo "::respawn:/usr/sbin/sleepradiopi-station" >> "${TARGET_DIR}/etc/inittab"

# Shut down when the station's web page asks (runs as root; see the script).
grep -q power-request-watch "${TARGET_DIR}/etc/inittab" ||
	echo "::respawn:/usr/sbin/power-request-watch" >> "${TARGET_DIR}/etc/inittab"
grep -q voice-install-watch "${TARGET_DIR}/etc/inittab" ||
	echo "::respawn:/usr/sbin/voice-install-watch" >> "${TARGET_DIR}/etc/inittab"
# dnsmasq only runs for the hotspot, started by the Wi-Fi manager.
rm -f "${TARGET_DIR}/etc/init.d/S80dnsmasq"
grep -q update-watch "${TARGET_DIR}/etc/inittab" ||
	echo "::respawn:/usr/sbin/update-watch" >> "${TARGET_DIR}/etc/inittab"
