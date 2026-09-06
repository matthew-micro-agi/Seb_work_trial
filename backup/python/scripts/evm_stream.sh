#!/bin/sh -e
# Run ON THE EVM. Configure the IMX296 pipeline, then camera -> NV12 -> Wave5 H.265 -> TCP server
# (Annex-B byte stream). The laptop viewer connects through an SSH port forward (scripts/camera_view.sh),
# so nothing needs opening on the laptop firewall.
#
#   scripts/evm_stream.sh [bind-ip] [port] [--color]      default 127.0.0.1 5600, full-res luma
#
# Env: FPS (30), EXPOSURE (lines, 1104 ~ 16 ms), GAIN (0..480 = 0..48 dB), BITRATE (bit/s, 6000000), MEDIA (/dev/media0)
HOST=${1:-127.0.0.1}; PORT=${2:-5600}; MODE=${3:-}
FPS=${FPS:-30}; EXPOSURE=${EXPOSURE:-1104}; GAIN=${GAIN:-0}; BITRATE=${BITRATE:-6000000}; MEDIA=${MEDIA:-/dev/media0}
cd "$(dirname "$0")/.."

SENSOR=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \(imx296 [0-9a-f-]*\).*/\1/p' | head -1)
[ -n "$SENSOR" ] || { echo "no imx296 entity on $MEDIA (overlay not applied? dmesg | grep imx296)"; exit 1; }
SUBDEV=$(media-ctl -d $MEDIA -e "$SENSOR")
FMT=$(media-ctl -d $MEDIA --get-v4l2 "'$SENSOR':0" | sed -n 's/.*fmt:\([A-Z0-9_]*\)\/\([0-9]*x[0-9]*\).*/\1\/\2/p')
CODE=${FMT%%/*}; SIZE=${FMT#*/}; W=${SIZE%x*}; H=${SIZE#*x}
case $CODE in SBGGR10_1X10) PIX=BG10;; SRGGB10_1X10) PIX=RG10;; SGBRG10_1X10) PIX=GB10;; SGRBG10_1X10) PIX=BA10;; Y10_1X10) PIX="Y10 ";; *) echo "unknown code $CODE"; exit 1;; esac
BRIDGE=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \(cdns_csi2rx[^ ]*\).*/\1/p' | head -1)
SHIM=$(media-ctl -d $MEDIA -p | sed -n 's/^- entity [0-9]*: \([0-9a-f]*\.ticsi2rx\) .*/\1/p' | head -1)
VIDEO=$(media-ctl -d $MEDIA -p | sed -n "/entity [0-9]*: $SHIM context 0/,/device node/s/.*device node name \(\/dev\/video[0-9]*\).*/\1/p" | head -1)

# the same format on every pad of the graph, then the capture node; frame rate via vertical blanking
for pad in "'$SENSOR':0" "'$BRIDGE':0" "'$BRIDGE':1" "'$SHIM':0" "'$SHIM':1"; do media-ctl -d $MEDIA -V "$pad [fmt:$FMT field:none]"; done
v4l2-ctl -d $VIDEO --set-fmt-video=width=$W,height=$H,pixelformat="$PIX" >/dev/null
HBLANK=$(v4l2-ctl -d $SUBDEV --get-ctrl horizontal_blanking | awk '{print $2}')
PIXRATE=$(v4l2-ctl -d $SUBDEV --get-ctrl pixel_rate | awk '{print $2}')
VBLANK=$(awk -v w=$W -v h=$H -v hb=$HBLANK -v pr=$PIXRATE -v fps=$FPS 'BEGIN{ printf "%d", pr/((w+hb)*fps) - h }')
v4l2-ctl -d $SUBDEV --set-ctrl vertical_blanking=$VBLANK,exposure=$EXPOSURE,analogue_gain=$GAIN
echo "sensor $SENSOR $FMT -> $VIDEO ($PIX), vblank $VBLANK for $FPS fps, exposure $EXPOSURE lines, gain $GAIN" >&2

exec python3 -m camsync.gststream --device $VIDEO --width $W --height $H --fourcc "$PIX" --fps $FPS --bitrate $BITRATE --bind $HOST --port $PORT $MODE
