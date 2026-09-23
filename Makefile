# Thin wrapper so `make` in this directory builds the image.
# Output goes to ./output, downloads to ./dl (both git-ignored).

BR       := $(CURDIR)/buildroot
O        := $(CURDIR)/output
DEFCONFIG := sleepradiopi_zero2w_defconfig
BRMAKE   := $(MAKE) -C $(BR) O=$(O) BR2_EXTERNAL=$(CURDIR)

all: $(O)/.config
	$(BRMAKE)

$(O)/.config:
	$(BRMAKE) $(DEFCONFIG)

# Re-read the defconfig after editing it.
defconfig:
	$(BRMAKE) $(DEFCONFIG)

# Save menuconfig changes back into configs/.
savedefconfig:
	$(BRMAKE) savedefconfig BR2_DEFCONFIG=$(CURDIR)/configs/$(DEFCONFIG)

# Anything else (menuconfig, linux-menuconfig, clean, <pkg>-rebuild ...)
%:
	$(BRMAKE) $@

# Stop the catch-all from trying to rebuild this Makefile.
Makefile: ;

.PHONY: all defconfig savedefconfig
