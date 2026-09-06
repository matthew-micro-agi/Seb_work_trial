#!/bin/sh -e
# The conformance suite against tile-agent on this PC with kpipe-synth as the recorder (TESTING.md).
# Ring in /dev/shm, 10 MB budget so C7 wraps in about a minute; the hooks kill the recorder, restart the
# agent without its index, and run ringfsck. Needs segfile built (cmake -S segfile -B segfile/build).
# C11 kills every kpipe-synth (or, with LIVE=1, every kpipe) on this machine: do not run it next to another.
#
# LIVE=1 uses the real kpipe on the synthetic source instead, with both encoder branches, so the node
# declares `live` and C15 checks the stream as well as /metrics. Needs pipeline built and x265enc/x264enc.
#
#   tileagent/tests/conformance_pc.sh [PORT] [RING] [SEGFILE_BUILD]
#   LIVE=1 tileagent/tests/conformance_pc.sh
cd "$(dirname "$0")/../.."
PORT=${1:-18090}; RING=${2:-/dev/shm/ring-conf}; B=${3:-segfile/build}
PY=${PYTHON:-python3}
[ -x "$B/kpipe-synth" ] && [ -x "$B/ringfsck" ] || { echo "build segfile first: cmake -S segfile -B $B && cmake --build $B" >&2; exit 2; }
if [ -n "$LIVE" ]; then
  [ -x pipeline/build/kpipe ] || { echo "build the pipeline first: cmake -S pipeline -B pipeline/build && cmake --build pipeline/build" >&2; exit 2; }
  RECORDER="--proxy --recorder"; RECORDER_CMD="pipeline/build/kpipe --fake --enc x265enc --proxy-enc x264enc"; VICTIM=kpipe
else
  RECORDER="--no-proxy --recorder"; RECORDER_CMD="$B/kpipe-synth"; VICTIM=kpipe-synth
fi
rm -rf "$RING"; mkdir -p "$RING"
AGENT="$PY -m tileagent --ring $RING --port $PORT --budget 10M --bitrate 2000000 --segment-s 2 $RECORDER"
$AGENT "$RECORDER_CMD" 2>"$RING.agent.log" &
PID=$!
cleanup() { RC=$?; set +e; kill $PID 2>/dev/null; kill $(cat "$RING.pid" 2>/dev/null) 2>/dev/null; sleep 0.5; pkill -x "$VICTIM" 2>/dev/null; exit $RC; }
trap cleanup EXIT
sleep 1
# --restart-agent: stop the agent (SIGTERM stops its recorder first), drop the index, start it again
cat > "$RING.restart.sh" <<EOF
#!/bin/sh
pkill -TERM -f "^$PY -m tileagent --ring $RING " || true
for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "^$PY -m tileagent --ring $RING " >/dev/null || break; sleep 0.5; done
rm -f "$RING/index.db" "$RING/index.db-wal" "$RING/index.db-shm"
$AGENT "$RECORDER_CMD" 2>>"$RING.agent.log" &
echo \$! > "$RING.pid"
sleep 1
EOF
chmod +x "$RING.restart.sh"
$PY icd/conformance.py 127.0.0.1:$PORT --segment-s 2 --kill-recorder "pkill -9 -x $VICTIM" \
    --restart-agent "$RING.restart.sh" --fsck "$B/ringfsck $RING" --wrap-timeout ${WRAP_TIMEOUT:-600}
