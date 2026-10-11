################################################################################
#
# wsprd
#
# Steve Franke (K9AN) and Joe Taylor (K1JT)'s WSPR decoder, as it was in
# WSJT-X 1.9.1: the last release in which it is C alone (later ones add a
# second decoder in Fortran, which this toolchain doesn't have). Only wsprd
# is built, from lib/wsprd in the archive inside the release's archive; the
# app runs it on a two-minute slot of a receiver's sound
# (app/sleepradiopi/audio/ft8.py).
#
################################################################################

WSPRD_VERSION = 1.9.1
WSPRD_SOURCE = wsjtx-$(WSPRD_VERSION).tgz
WSPRD_SITE = https://downloads.sourceforge.net/project/wsjt/wsjtx-$(WSPRD_VERSION)
WSPRD_LICENSE = GPL-3.0+
WSPRD_LICENSE_FILES = COPYING
WSPRD_DEPENDENCIES = fftw-single

define WSPRD_EXTRACT_CMDS
	$(TAR) -xzOf $(WSPRD_DL_DIR)/$(WSPRD_SOURCE) wsjtx-$(WSPRD_VERSION)/src/wsjtx.tgz | \
		$(TAR) -xzf - -C $(@D) --strip-components=3 wsjtx/lib/wsprd
	$(TAR) -xzOf $(WSPRD_DL_DIR)/$(WSPRD_SOURCE) wsjtx-$(WSPRD_VERSION)/src/wsjtx.tgz | \
		$(TAR) -xzf - -C $(@D) --strip-components=1 wsjtx/COPYING
	rm -f $(@D)/fftw3.h
endef

# (its own Makefile is for the machine it's on: the same seven files, by hand. -fcommon: it was written when a
# variable defined in a header and seen by two files was one variable; compilers since 2020 say it's two.)
define WSPRD_BUILD_CMDS
	cd $(@D) && $(TARGET_CC) $(TARGET_CFLAGS) -O3 -ffast-math -fcommon -Wno-missing-braces -o wsprd \
		wsprd.c wsprsim_utils.c wsprd_utils.c tab.c fano.c jelinek.c nhash.c $(TARGET_LDFLAGS) -lfftw3f -lm
endef

define WSPRD_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/wsprd $(TARGET_DIR)/usr/bin/wsprd
endef

$(eval $(generic-package))
