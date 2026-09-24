################################################################################
#
# python-sherpa-onnx
#
# The prebuilt PyPI wheels, unpacked into site-packages. Building sherpa-onnx
# and onnxruntime from source on this toolchain would take hours for no gain:
# the manylinux2014 wheels only need glibc >= 2.17.
#
################################################################################

PYTHON_SHERPA_ONNX_VERSION = 1.13.8
PYTHON_SHERPA_ONNX_SITE = https://files.pythonhosted.org/packages/4f/9a/51821829b5735b3d7ce607992a62fe93d5f7215cda324c11191c77d49b9b
PYTHON_SHERPA_ONNX_SOURCE = sherpa_onnx-$(PYTHON_SHERPA_ONNX_VERSION)-cp$(subst .,,$(PYTHON3_VERSION_MAJOR))-cp$(subst .,,$(PYTHON3_VERSION_MAJOR))-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
PYTHON_SHERPA_ONNX_CORE_WHEEL = sherpa_onnx_core-$(PYTHON_SHERPA_ONNX_VERSION)-py3-none-manylinux2014_aarch64.whl
PYTHON_SHERPA_ONNX_EXTRA_DOWNLOADS = \
	https://files.pythonhosted.org/packages/9c/72/02227b14cd3fb3d504a8814ad144ed321a53331948c893821e02f4abb1d7/$(PYTHON_SHERPA_ONNX_CORE_WHEEL)
PYTHON_SHERPA_ONNX_LICENSE = Apache-2.0
PYTHON_SHERPA_ONNX_DEPENDENCIES = host-python3 python3

# Wheels are zip files; Buildroot doesn't know the .whl suffix.
define PYTHON_SHERPA_ONNX_EXTRACT_CMDS
	$(HOST_DIR)/bin/python3 -m zipfile -e $(PYTHON_SHERPA_ONNX_DL_DIR)/$(PYTHON_SHERPA_ONNX_SOURCE) $(@D)
	$(HOST_DIR)/bin/python3 -m zipfile -e $(PYTHON_SHERPA_ONNX_DL_DIR)/$(PYTHON_SHERPA_ONNX_CORE_WHEEL) $(@D)
endef

# Leave out the C/C++ API libraries and headers (only for C programs; the
# Python module links onnxruntime directly) and the command-line tools.
define PYTHON_SHERPA_ONNX_INSTALL_TARGET_CMDS
	rm -rf $(@D)/sherpa_onnx/include \
		$(@D)/sherpa_onnx/lib/libsherpa-onnx-c-api.so \
		$(@D)/sherpa_onnx/lib/libsherpa-onnx-cxx-api.so
	mkdir -p $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages
	cp -a $(@D)/sherpa_onnx $(@D)/sherpa_onnx.libs \
		$(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/
endef

$(eval $(generic-package))
