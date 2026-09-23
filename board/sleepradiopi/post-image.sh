#!/bin/bash
# Assemble sdcard.img from the boot files and the rootfs.
set -e

BOARD_DIR="$(dirname "$0")"
LOCAL="${BR2_EXTERNAL_SLEEPRADIOPI_PATH}/local"
GENIMAGE_CFG="${BINARIES_DIR}/genimage.cfg"
GENIMAGE_TMP="${BUILD_DIR}/genimage.tmp"

FILES=()
for i in "${BINARIES_DIR}"/*.dtb "${BINARIES_DIR}"/rpi-firmware/*; do
	FILES+=( "${i#${BINARIES_DIR}/}" )
done
FILES+=( "Image" )

# Wi-Fi credentials go on the boot partition so they can be edited on a PC.
if [ -f "${LOCAL}/wpa_supplicant.conf" ]; then
	cp "${LOCAL}/wpa_supplicant.conf" "${BINARIES_DIR}/wpa_supplicant.conf"
	FILES+=( "wpa_supplicant.conf" )
else
	echo "WARNING: no local/wpa_supplicant.conf -- the image won't join Wi-Fi" >&2
	echo "         until you add wpa_supplicant.conf to the boot partition." >&2
	rm -f "${BINARIES_DIR}/wpa_supplicant.conf"
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
