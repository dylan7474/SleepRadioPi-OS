################################################################################
#
# ft8-lib
#
# Karlis Goba's FT8 / FT4 library: only its decode_ft8 program is built and
# installed (the app runs it on a slot of a receiver's sound: see
# app/sleepradiopi/audio/ft8.py). The source archive is large for what it is
# (22 MB): it carries recordings to test against.
#
################################################################################

FT8_LIB_VERSION = 9fec6ca39886edbf96f4f5e71edc76da5074e871
FT8_LIB_SITE = $(call github,kgoba,ft8_lib,$(FT8_LIB_VERSION))
FT8_LIB_LICENSE = MIT
FT8_LIB_LICENSE_FILES = LICENSE

# Its Makefile sets CFLAGS itself: given here they replace those, so what it
# needs of its own (-DHAVE_STPCPY -I.) comes too.
define FT8_LIB_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) CC="$(TARGET_CC)" AR="$(TARGET_AR)" \
		CFLAGS="$(TARGET_CFLAGS) -O3 -DHAVE_STPCPY -I." decode_ft8
endef

define FT8_LIB_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/decode_ft8 $(TARGET_DIR)/usr/bin/decode_ft8
endef

$(eval $(generic-package))
