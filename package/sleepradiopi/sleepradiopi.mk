################################################################################
#
# sleepradiopi
#
# To build from a local checkout while developing, put this in output/local.mk:
#   SLEEPRADIOPI_OVERRIDE_SRCDIR = /path/to/SleepRadioPi
#
################################################################################

SLEEPRADIOPI_VERSION = 4ca2f573f6612658a1b0564d430dd4b28a6675e0
SLEEPRADIOPI_SITE = $(call github,dylan7474,SleepRadioPi,$(SLEEPRADIOPI_VERSION))
SLEEPRADIOPI_LICENSE = GPL-3.0-or-later
SLEEPRADIOPI_LICENSE_FILES = LICENSE NOTICE
SLEEPRADIOPI_DEPENDENCIES = python3

# Pure Python with no build step: just copy the package. Buildroot's python3
# finalise step compiles it to .pyc (the root is read-only at run time).
define SLEEPRADIOPI_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/sleepradiopi
	cp -a $(@D)/sleepradiopi $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/
endef

$(eval $(generic-package))
