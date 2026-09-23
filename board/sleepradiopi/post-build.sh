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

# No console on the HDMI port: it's headless, and it saves a getty.
sed -i '/^tty1::/d' "${TARGET_DIR}/etc/inittab"
