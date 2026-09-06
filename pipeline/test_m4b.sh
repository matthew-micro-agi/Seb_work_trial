#!/bin/sh
# M4b exit test (PLAN_K_MATCHING.md): kpipe on the synthetic source with two encoder branches — the H.265
# recording and the H.264 proxy substream of DESIGN_RING_CONTROL.md §8.3a — the proxy valve toggled every
# TOGGLE seconds, then the checkers. Run from anywhere; needs pipeline/build/kpipe.
#
#   pipeline/test_m4b.sh [FRAMES]          default 10000 (5.6 min at 30 fps)
#   env: ENC (x265enc on the PC, v4l2h265enc on the board), PENC (x264enc / v4l2h264enc), FPS 30,
#        W 1456, H 1088, PW 728, PH 544, PFPS 15, PBITRATE 1000000, SCALER (videoscale / tiovxmultiscaler),
#        TOGGLE 10, OUT /tmp/m4b,
#        PIXELS=1 decodes both files and compares the barcode in the picture with the SEI (needs avdec_*: PC)
cd "$(dirname "$0")/.." || exit 2
FRAMES=${1:-10000}; ENC=${ENC:-x265enc}; PENC=${PENC:-x264enc}; FPS=${FPS:-30}; W=${W:-1456}; H=${H:-1088}
PW=${PW:-728}; PH=${PH:-544}; PFPS=${PFPS:-15}; PBITRATE=${PBITRATE:-1000000}; SCALER=${SCALER:-videoscale}
TOGGLE=${TOGGLE:-10}; OUT=${OUT:-/tmp/m4b}
mkdir -p "$OUT"
pipeline/build/kpipe --fake --enc "$ENC" --fps "$FPS" --period-ns $((1000000000 / FPS)) --width "$W" --height "$H" \
  --out "$OUT/rec.h265" --log "$OUT/rec.jsonl" \
  --proxy-out "$OUT/proxy.h264" --proxy-sock "$OUT/live.sock" --proxy-enc "$PENC" --proxy-scaler "$SCALER" \
  --proxy-width "$PW" --proxy-height "$PH" --proxy-fps "$PFPS" --proxy-bitrate "$PBITRATE" \
  --frames "$FRAMES" 2>"$OUT/kpipe.log" &
PID=$!
while sleep "$TOGGLE" && kill -0 "$PID" 2>/dev/null; do kill -USR1 "$PID" 2>/dev/null; done
wait "$PID"; RC=$?
grep -c "proxy valve open" "$OUT/kpipe.log" | sed 's/^/valve opened /'
tail -n 1 "$OUT/kpipe.log"
python3 pipeline/check_m4b.py "$OUT/rec.h265" "$OUT/rec.jsonl" --proxy "$OUT/proxy.h264" ${PIXELS:+--pixels} || RC=1
(cd backup/python && python3 -m camsync.check_stream "$OUT/rec.h265" "$OUT/rec.jsonl") || RC=1
[ "$RC" = 0 ] && echo "M4B PASS" || echo "M4B FAIL (kpipe or a checker returned non-zero)"
exit $RC
