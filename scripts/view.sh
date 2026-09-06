#!/bin/sh
# One command on the laptop: start kpipe on the EVM with its TCP output and show the stream here.
#   scripts/view.sh [port] [extra kpipe options...]      e.g. scripts/view.sh 5600 --out /tmp/rec.h265 --log /tmp/rec.jsonl
# Ctrl-C stops both ends. Transport: H.265 byte stream over TCP through an SSH port forward (no firewall rule).
PORT=${1:-5600}; [ $# -gt 0 ] && shift
HOST=${EVM:-evm}
SSH="ssh -o ControlMaster=no -o ControlPath=none"
STALE=$(ss -ltnp 2>/dev/null | sed -n "s/.*127.0.0.1:$PORT .*pid=\([0-9]*\).*/\1/p" | head -1)
[ -n "$STALE" ] && { echo "port $PORT held by pid $STALE, closing it"; kill $STALE; sleep 0.5; }
cleanup() { $SSH $HOST 'pkill -x kpipe' 2>/dev/null; kill $SSHPID 2>/dev/null; }
trap cleanup EXIT INT TERM
$SSH -L $PORT:127.0.0.1:$PORT $HOST "cd /opt/ti_workspace && pkill -x kpipe; exec pipeline/kpipe.sh --tcp $PORT --out /dev/null --log /dev/null $*" &
SSHPID=$!
sleep 4
${VIEWER:-gst-launch-1.0} tcpclientsrc host=127.0.0.1 port=$PORT ! queue ! h265parse ! avdec_h265 ! videoconvert ! fpsdisplaysink text-overlay=true sync=false
