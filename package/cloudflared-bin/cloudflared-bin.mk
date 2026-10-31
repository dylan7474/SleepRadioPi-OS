################################################################################
#
# cloudflared-bin
#
# Cloudflare's official static linux-arm64 binary. Building it from source
# needs a Go toolchain and a big download for no gain. It is installed but
# does nothing by itself: remote-access-watch starts it only when the web page
# has switched Remote access on and holds a tunnel token.
#
################################################################################

CLOUDFLARED_BIN_VERSION = 2026.10.7
CLOUDFLARED_BIN_SOURCE = cloudflared-linux-arm64
CLOUDFLARED_BIN_SITE = https://github.com/cloudflare/cloudflared/releases/download/$(CLOUDFLARED_BIN_VERSION)
CLOUDFLARED_BIN_LICENSE = Apache-2.0

# A single file, not an archive.
define CLOUDFLARED_BIN_EXTRACT_CMDS
	cp $(CLOUDFLARED_BIN_DL_DIR)/$(CLOUDFLARED_BIN_SOURCE) $(@D)/cloudflared
endef

define CLOUDFLARED_BIN_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/cloudflared $(TARGET_DIR)/usr/bin/cloudflared
endef

$(eval $(generic-package))
