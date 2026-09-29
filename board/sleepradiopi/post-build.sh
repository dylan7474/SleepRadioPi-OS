#!/bin/sh
# Runs after the target tree is assembled, before the rootfs image is made.
set -eu

LOCAL="${BR2_EXTERNAL_SLEEPRADIOPI_PATH}/local"

# No SSH keys in the root: the same squashfs goes into public releases. The
# host keys are made on the radio's first boot and kept in /data/ssh, with
# the authorised login keys (S49sshkeys). The ssh tree can't hold them.
rm -f "${TARGET_DIR}"/etc/ssh/ssh_host_* "${TARGET_DIR}/root/.ssh/authorized_keys"

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
grep -q media-rw-watch "${TARGET_DIR}/etc/inittab" ||
	echo "::respawn:/usr/sbin/media-rw-watch" >> "${TARGET_DIR}/etc/inittab"
