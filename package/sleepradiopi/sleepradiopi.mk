################################################################################
#
# sleepradiopi
#
# The radio software, from app/ in this repo. `make sleepradiopi-rebuild`
# (or a plain `make` after `make sleepradiopi-dirclean`) picks up changes.
#
################################################################################

SLEEPRADIOPI_VERSION = local
SLEEPRADIOPI_SITE = $(BR2_EXTERNAL_SLEEPRADIOPI_PATH)/app
SLEEPRADIOPI_SITE_METHOD = local
# Leave development leftovers out of the build copy
SLEEPRADIOPI_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	--exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude voices
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
