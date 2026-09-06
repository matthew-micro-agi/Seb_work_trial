#!/bin/bash
# Build a patched driver module for a TI kernel tree, out of tree, vermagic matching the board.
#
#   KERNEL_DIR   TI kernel checkout (default: ../../build/ti-linux-kernel = 6.6.44, tag 10.01.08)
#   TOOLCHAIN    aarch64 cross toolchain bin dir (Arm GNU 13.3 used; any aarch64 gcc works)
#   KREL         target kernel release string, e.g. 6.6.44-ti-01478-g541c20281af7-dirty (default) or
#                6.12.17-ti-00773-gcdcaeac783e3-dirty (Edge AI SDK 11 card)
#   BOARD_CONFIG optional saved /proc/config.gz of the board (used for 6.6); otherwise
#                `make defconfig ti_arm64_prune.config`, which is what TI's 6.12 build does
#
#   kernel/build.sh module      -> kernel/out/imx296-<KREL>.ko          (patch 0001)
#   kernel/build.sh csi2rx      -> kernel/out/j721e-csi2rx-<KREL>.ko    (patch 0003, 6.12 tree)
#   kernel/build.sh all         -> Image, dtbs, modules (long)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
KERNEL_DIR=${KERNEL_DIR:-$HERE/../../build/ti-linux-kernel}
TOOLCHAIN=${TOOLCHAIN:-$HERE/../../build/arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu/bin}
KREL=${KREL:-6.6.44-ti-01478-g541c20281af7-dirty}
BOARD_CONFIG=${BOARD_CONFIG:-$HERE/configs/j722s-evm-board-6.6.44.config}
export PATH="$TOOLCHAIN:$PATH" ARCH=arm64 CROSS_COMPILE=aarch64-none-linux-gnu-
JOBS=${JOBS:-$(nproc)}
cd "$KERNEL_DIR"
BASE=$(make -s kernelversion)          # e.g. 6.6.44 or 6.12.17
LOCALVER=${KREL#$BASE}                  # -ti-01478-g...-dirty
[ "$LOCALVER" != "$KREL" ] || { echo "KREL $KREL does not start with this tree's version $BASE"; exit 1; }
apply() {   # apply one of the patches next to this script, unless the tree already has it
  if git apply --check "$HERE/$1" 2>/dev/null; then
    patch -p1 < "$HERE/$1"
  else
    echo "$1 already applied (or does not apply): check $KERNEL_DIR"
  fi
}
apply 0001-media-imx296-external-trigger-mode.patch
if [ "${1:-module}" = csi2rx ]; then apply 0003-media-j721e-csi2rx-timestamp-at-frame-start.patch; fi
if [ ! -f .config ] || [ "$2" = "--reconfig" ]; then
  if [ "$BASE" = 6.6.44 ] && [ -f "$BOARD_CONFIG" ]; then cp "$BOARD_CONFIG" .config; else make -s defconfig ti_arm64_prune.config; fi
  scripts/config --set-str LOCALVERSION "$LOCALVER"
  scripts/config --disable LOCALVERSION_AUTO
  scripts/config --module VIDEO_IMX296
  scripts/config --module VIDEO_TI_J721E_CSI2RX
  make -s olddefconfig
fi
make -s syncconfig   # setlocalversion reads include/config/auto.conf
[ "$(make -s kernelrelease)" = "$KREL" ] || { echo "kernelrelease $(make -s kernelrelease) != $KREL"; exit 1; }
case "${1:-module}" in
  module)
    make -s -j"$JOBS" modules_prepare
    make -j"$JOBS" KBUILD_MODPOST_WARN=1 drivers/media/i2c/imx296.ko
    mkdir -p "$HERE/out"; ${CROSS_COMPILE}strip --strip-debug -o "$HERE/out/imx296-$KREL.ko" drivers/media/i2c/imx296.ko
    modinfo "$HERE/out/imx296-$KREL.ko" | grep -E "^(filename|vermagic)";;
  csi2rx)
    KO=drivers/media/platform/ti/j721e-csi2rx/j721e-csi2rx.ko
    make -s -j"$JOBS" modules_prepare
    make -j"$JOBS" KBUILD_MODPOST_WARN=1 "$KO"   # no vmlinux.o out of tree, so modpost cannot resolve
    mkdir -p "$HERE/out"; ${CROSS_COMPILE}strip --strip-debug -o "$HERE/out/j721e-csi2rx-$KREL.ko" "$KO"
    modinfo "$HERE/out/j721e-csi2rx-$KREL.ko" | grep -E "^(filename|vermagic)";;
  all)
    time make -j"$JOBS" Image dtbs modules; make -s kernelrelease;;
esac
