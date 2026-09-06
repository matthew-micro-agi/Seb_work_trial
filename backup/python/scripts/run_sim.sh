#!/bin/sh -e
# Usage: scripts/run_sim.sh virtual|wired [frames] [faults]
#   virtual  fake camera on its own timer, no wires needed
#   wired    fake camera triggered by the PWM loopback (J28.32 -> 37), strobe on J28.36 -> 22;
#            run scripts/trigger.sh on first
# frames=0 runs until interrupted. Output in ./out (override with OUT=).
cd "$(dirname "$0")/.."
MODE=${1:-virtual}
FRAMES=${2:-900}
FAULTS=${3:-skip@100,drop@200,dup@300,late@400:40,skip@500:3}
FPS=30
OUT=${OUT:-out}
mkdir -p "$OUT"
case "$MODE" in
  virtual) CAM="--fps $FPS"; BIND="" ;;
  wired)   CAM="--fsin-line 36 --strobe-line 41"; BIND="--trigger-line 33 --strobe-line 42" ;;
  *) echo "mode must be virtual or wired"; exit 2 ;;
esac
python3 -m camsync.fakecam $CAM --frames "$FRAMES" --counter-start 65400 --faults "$FAULTS" \
  | python3 -m camsync.binder --fps $FPS --check-s 10 $BIND --out "$OUT/rec.h265" --log "$OUT/rec.jsonl"
python3 -m camsync.check_stream "$OUT/rec.h265" "$OUT/rec.jsonl"
echo "== events"
grep -E '"ev": "(anchor|gap|dup|k_corrected|trigger_stall|trigger_resumed|strobe_mismatch|rearm)"' "$OUT/rec.jsonl"
