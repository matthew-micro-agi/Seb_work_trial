#!/bin/sh
# Run ON THE LAPTOP. Shows the H.265 byte stream from a TCP port with an fps readout top-left.
#   scripts/view_stream.sh [port] [host]      default 5600 on 127.0.0.1 (the SSH forward)
PORT=${1:-5600}; HOST=${2:-127.0.0.1}
exec gst-launch-1.0 tcpclientsrc host=$HOST port=$PORT ! queue ! h265parse ! avdec_h265 ! videoconvert \
  ! fpsdisplaysink text-overlay=true sync=false
