#!/bin/sh -e
# The failure of OPEN.md §1c and its fix, on the PC, with no hardware: two recording sessions over one
# pulse train that never stops. Without a beacon the second session's K restarts near zero; with one it
# carries on from the source. Needs segfile built (cmake -S segfile -B segfile/build && cmake --build …).
#
#   tileagent/tests/beacon_pc.sh [PORT] [RING] [SEGFILE_BUILD]
#
# The pulse source is faked here — the PC has no PTP clock and no ePWM — by a writer that appends the
# same trigger-log lines tileagent/beacon.py's TriggerSource would, once a second, off a 30 Hz grid that
# started before the first session and runs past the last. Everything downstream of that file is the
# real thing: the real GET /v1/trigger/log with its Range read, the real relay, the real recorder stdin,
# the real kmatch::CapturedEdges::beacon(), the real SEI records, the real ringfsck.
cd "$(dirname "$0")/../.."
PORT=${1:-18092}; RING=${2:-${TMPDIR:-/dev/shm}/ring-beacon}; B=${3:-segfile/build}
PY=${PYTHON:-python3}
RUN_S=${RUN_S:-8}
[ -x "$B/kpipe-synth" ] && [ -x "$B/ringfsck" ] || { echo "build segfile first: cmake -S segfile -B $B && cmake --build $B" >&2; exit 2; }
T=${TMPDIR:-/dev/shm}

cat > "$T/fake_trigger_source.py" <<'EOF'
"""The pulse source this machine has not got: trigger.log lines off a 30 Hz grid, one a second."""
import json, sys, time
path, epoch, period = sys.argv[1], int(sys.argv[2]), 33_333_333
t0 = time.monotonic_ns()
while True:
    k = (time.monotonic_ns() - t0) // period
    with open(path, "a") as f:
        f.write(json.dumps({"epoch": epoch, "k": k, "ptp_ns": str(t0 + k * period), "utc_ns": None},
                           separators=(",", ":")) + "\n")
    time.sleep(1)
EOF

cat > "$T/ring_k.py" <<'EOF'
"""The (epoch, K) span of every segment of a ring, and the largest K in it."""
import os, sys
sys.path.insert(0, "sei/python")
import sei
ring, top = sys.argv[1], 0
for name in sorted(os.listdir(os.path.join(ring, "seg"))):
    with open(os.path.join(ring, "seg", name), "rb") as f:
        recs = [r for r in (sei.record_of(n) for _, _, n in sei.split(f.read())) if r]
    a, b = recs[0], recs[-1]
    top = max(top, b[1])
    print("  %s  epoch %d K %d -> epoch %d K %d%s" % (name, a[0], a[1], b[0], b[1],
                                                      "   [head K_LOCAL]" if a[4] & (1 << 9) else ""))
print("highest K in the ring: %d" % top)
EOF

# One run: $1 = off|on (the beacon), $2 = the ring. Two sessions over one uninterrupted pulse train.
AG=""; SRC=""
cleanup() { [ -n "$AG" ] && kill $AG 2>/dev/null; [ -n "$SRC" ] && kill $SRC 2>/dev/null; true; }
trap cleanup EXIT

run() {
  rm -rf "$2"; mkdir -p "$2"
  $PY "$T/fake_trigger_source.py" "$2/trigger.log" 1 & SRC=$!
  sleep 0.5
  opt=""; [ "$1" = on ] && opt="--beacon-src http://127.0.0.1:$PORT/v1/trigger/log"
  $PY -m tileagent --ring "$2" --port $PORT --budget 40M --bitrate 2000000 --segment-s 2 \
      --recorder "$B/kpipe-synth" $opt >"$2.agent.log" 2>&1 & AG=$!
  sleep 1.5
  for s in A B; do
    curl -sf -H 'Content-Type: application/json' -d '{"edges":"GRID","latencyNs":"0"}' localhost:$PORT/tile.v1.Tile/StartRecording >/dev/null
    sleep $RUN_S
    curl -sf -d '{}' localhost:$PORT/tile.v1.Tile/StopRecording >/dev/null
    sleep 1
  done
  kill $AG $SRC 2>/dev/null; AG=""; SRC=""; sleep 0.5
}

echo "== the failure: no beacon (OPEN.md §1c) =="
run off "$RING-off"
$PY "$T/ring_k.py" "$RING-off" | tee "$T/off.txt"
"$B/ringfsck" "$RING-off" || true

echo
echo "== the fix: the beacon relayed from this node's own /v1/trigger/log =="
run on "$RING-on"
$PY "$T/ring_k.py" "$RING-on" | tee "$T/on.txt"
"$B/ringfsck" "$RING-on"
grep -ho '"ev":"[a-z_]*"' "$RING-on"/logs/*.jsonl | grep -c beacon_ok | sed 's/^/beacon_ok lines: /'

# Two sessions of RUN_S seconds at 30 fps over one train: the source is past 2 * RUN_S * 30 pulses by
# the end. Without the beacon the ring never gets past one session's worth.
ONE=$((RUN_S * 30 + 60))
OFF=$(sed -n 's/^highest K in the ring: //p' "$T/off.txt")
ON=$(sed -n 's/^highest K in the ring: //p' "$T/on.txt")
echo
[ "$OFF" -lt "$ONE" ] || { echo "FAIL: without a beacon the ring should not get past $ONE, got $OFF" >&2; exit 1; }
[ "$ON" -gt "$ONE" ] || { echo "FAIL: with the beacon session B should carry the source's K past $ONE, got $ON" >&2; exit 1; }
echo "beacon_pc: K stops at $OFF without the beacon and reaches $ON with it; ringfsck clean"
