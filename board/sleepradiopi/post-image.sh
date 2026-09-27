#!/bin/bash
# Assemble sdcard.img from the boot files and the rootfs.
set -e

BOARD_DIR="$(dirname "$0")"
LOCAL="${BR2_EXTERNAL_SLEEPRADIOPI_PATH}/local"
GENIMAGE_CFG="${BINARIES_DIR}/genimage.cfg"
GENIMAGE_TMP="${BUILD_DIR}/genimage.tmp"

# rpi-firmware installs these only when it's (re)built, so an edit to the
# board copies would otherwise be missed by an incremental build.
install -m 0644 "${BOARD_DIR}/config.txt" "${BOARD_DIR}/cmdline.txt" \
	"${BINARIES_DIR}/rpi-firmware/"

FILES=()
for i in "${BINARIES_DIR}"/*.dtb "${BINARIES_DIR}"/rpi-firmware/*; do
	FILES+=( "${i#${BINARIES_DIR}/}" )
done
FILES+=( "Image" )

# Your own card (not a public release): your Wi-Fi and your ssh login key go
# on the boot partition, where they can also be edited on a PC. A release
# (scripts/release.sh sets SLEEPRADIOPI_RELEASE) gets neither: a new radio
# joins Wi-Fi through its hotspot, and ssh stays closed until the builder
# puts an authorized_keys file on the boot partition.
rm -f "${BINARIES_DIR}/wpa_supplicant.conf" "${BINARIES_DIR}/authorized_keys"
if [ -n "${SLEEPRADIOPI_RELEASE:-}" ]; then
	echo "release build: no Wi-Fi settings or ssh keys on the card"
else
	for f in wpa_supplicant.conf authorized_keys; do
		if [ -f "${LOCAL}/$f" ]; then
			cp "${LOCAL}/$f" "${BINARIES_DIR}/$f"
			FILES+=( "$f" )
		else
			echo "note: no local/$f -- not on the card" >&2
		fi
	done
fi

BOOT_FILES=$(printf '\\t\\t\\t"%s",\\n' "${FILES[@]}")
sed "s|#BOOT_FILES#|${BOOT_FILES}|" "${BOARD_DIR}/genimage.cfg.in" \
	> "${GENIMAGE_CFG}"

trap 'rm -rf "${ROOTPATH_TMP}"' EXIT
ROOTPATH_TMP="$(mktemp -d)"
rm -rf "${GENIMAGE_TMP}"

genimage \
	--rootpath "${ROOTPATH_TMP}"   \
	--tmppath "${GENIMAGE_TMP}"    \
	--inputpath "${BINARIES_DIR}"  \
	--outputpath "${BINARIES_DIR}" \
	--config "${GENIMAGE_CFG}"
