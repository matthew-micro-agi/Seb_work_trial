#!/bin/sh
# Run ON THE LAPTOP. One command: start the camera stream on the EVM and show it here.
#   scripts/camera_view.sh [--color] [port]
# Needs "ssh evm" to work and the repo deployed (scripts/deploy.sh). Ctrl-C stops both ends.
# Transport: H.265 over TCP through an SSH port forward; a dedicated ssh connection (no control master),
# because a persisting master would keep a stale forward alive across runs.
MODE=""; PORT=5600
for a in "$@"; do case "$a" in --color) MODE=--color;; *) PORT=$a;; esac; done
HOST=${EVM:-evm}
SSH="ssh -o ControlMaster=no -o ControlPath=none"
VIEWER=${VIEWER:-"$(dirname "$0")/view_stream.sh"}
# a forward left behind by an earlier ssh (control master) blocks the port: clear it
STALE=$(ss -ltnp 2>/dev/null | sed -n "s/.*127.0.0.1:$PORT .*pid=\([0-9]*\).*/\1/p" | head -1)
[ -n "$STALE" ] && { echo "port $PORT held by pid $STALE, closing it"; kill $STALE; sleep 0.5; }
cleanup() { $SSH $HOST 'pkill -f "^python3 -m camsync"' 2>/dev/null; kill $SSHPID 2>/dev/null; }
trap cleanup EXIT INT TERM
$SSH -L $PORT:127.0.0.1:$PORT $HOST "cd /opt/ti_workspace/backup/python && pkill -f '^python3 -m camsync'; sleep 0.5; exec scripts/evm_stream.sh 127.0.0.1 $PORT $MODE" &
SSHPID=$!
sleep 4
$VIEWER $PORT
