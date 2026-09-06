#!/bin/sh -e
# Configure the IMX296 media graph and sensor, then run kpipe. Run ON THE EVM from the workspace root.
#
#   pipeline/kpipe.sh [kpipe options...]        e.g. pipeline/kpipe.sh --out /tmp/rec.h265 --log /tmp/rec.jsonl --frames 900
#
# Env: FPS (30), EXPOSURE (lines, 1104 ~ 16 ms), GAIN (0..480 = 0..48 dB), MEDIA (/dev/media0)
FPS=${FPS:-30}; EXPOSURE=${EXPOSURE:-1104}; GAIN=${GAIN:-0}; MEDIA=${MEDIA:-/dev/media0}
cd "$(dirname "$0")/.."
SENSOR=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \(imx296 [0-9a-f-]*\).*/\1/p' | head -1)
[ -n "$SENSOR" ] || { echo "no imx296 entity on $MEDIA (overlay not applied? dmesg | grep imx296)" >&2; exit 1; }
SUBDEV=$(media-ctl -d $MEDIA -e "$SENSOR")
FMT=$(media-ctl -d $MEDIA --get-v4l2 "'$SENSOR':0" | sed -n 's/.*fmt:\([A-Z0-9_]*\)\/\([0-9]*x[0-9]*\).*/\1\/\2/p')
SIZE=${FMT#*/}; W=${SIZE%x*}; H=${SIZE#*x}
BRIDGE=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \(cdns_csi2rx[^ ]*\).*/\1/p' | head -1)
SHIM=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \([0-9a-f]*\.ticsi2rx\) .*/\1/p' | head -1)
VIDEO=$(media-ctl -d $MEDIA -p | sed -n "/entity [0-9]*: $SHIM context 0/,/device node/s/.*device node name \(\/dev\/video[0-9]*\).*/\1/p" | head -1)
for pad in "'$SENSOR':0" "'$BRIDGE':0" "'$BRIDGE':1" "'$SHIM':0" "'$SHIM':1"; do media-ctl -d $MEDIA -V "$pad [fmt:$FMT field:none]"; done
HBLANK=$(v4l2-ctl -d $SUBDEV --get-ctrl horizontal_blanking | awk '{print $2}')
PIXRATE=$(v4l2-ctl -d $SUBDEV --get-ctrl pixel_rate | awk '{print $2}')
VBLANK=$(awk -v w=$W -v h=$H -v hb=$HBLANK -v pr=$PIXRATE -v fps=$FPS 'BEGIN{ printf "%d", pr/((w+hb)*fps) - h }')
# In trigger mode the exposure is the XTRIG low width, not the register: writing it changes nothing
# (OPEN.md §2, measured). Set it only when the sensor is free-running, so the log does not lie.
SNODE=$(cat /proc/device-tree/__symbols__/imx296_0 2>/dev/null | tr -d "\0")
if [ -n "$SNODE" ] && [ -e "/proc/device-tree$SNODE/trigger-mode" ]; then TRIGGERED=1; else TRIGGERED=0; fi
if [ $TRIGGERED = 1 ]; then
  v4l2-ctl -d $SUBDEV --set-ctrl vertical_blanking=$VBLANK,analogue_gain=$GAIN
else
  v4l2-ctl -d $SUBDEV --set-ctrl vertical_blanking=$VBLANK,exposure=$EXPOSURE,analogue_gain=$GAIN
fi
# the sensor's first stream-on after a power-up fails (HOWTO §5): keep it powered
I2C=$(readlink -f /sys/class/video4linux/$(basename $SUBDEV)/device 2>/dev/null); [ -n "$I2C" ] && echo on > $I2C/power/control 2>/dev/null || true
if [ $TRIGGERED = 1 ]; then
  echo "sensor $SENSOR $FMT -> $VIDEO, vblank $VBLANK for $FPS fps, gain $GAIN; triggered: exposure is the XTRIG low width (tools/trigger.sh), the exposure register is ignored" >&2
else
  echo "sensor $SENSOR $FMT -> $VIDEO, vblank $VBLANK for $FPS fps, exposure $EXPOSURE lines, gain $GAIN" >&2
fi
exec pipeline/build/kpipe --device $VIDEO --width $W --height $H --fps $FPS --period-ns $((1000000000 / FPS)) "$@"
