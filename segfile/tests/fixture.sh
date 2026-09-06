#!/bin/sh -e
# Build a fixture ring with kpipe-synth and check it with ringfsck. Also the ring tile-agent runs against
# on the PC (TESTING.md): 60 s at 30 fps in 2 s segments, 2 Mbps, 10 MB budget (the ring wraps), an epoch
# change at frame 845 (one split segment at the IDR at 870). ringfsck must report nothing.
#
#   segfile/tests/fixture.sh SYNTH RINGFSCK [OUT]      default OUT ${TMPDIR:-/tmp}/ring-fixture
SYNTH=$1; FSCK=$2; OUT=${3:-${TMPDIR:-/tmp}/ring-fixture}
rm -rf "$OUT"; mkdir -p "$OUT"
"$SYNTH" --ring "$OUT" --fps 30 --segment-s 2 --budget 10000000 --bitrate 2000000 --frames 1800 --fast --epoch-at 845 > "$OUT.jsonl"
CLOSED=$(grep -c segment_closed "$OUT.jsonl"); RECLAIMED=$(grep -c '"reclaimed"' "$OUT.jsonl")
[ "$CLOSED" -eq 31 ] || { echo "expected 31 segment_closed (30 + the epoch split), got $CLOSED" >&2; exit 1; }
[ "$RECLAIMED" -gt 0 ] || { echo "expected the ring to wrap" >&2; exit 1; }
"$FSCK" "$OUT"
echo "fixture ring: $OUT ($(ls "$OUT/seg" | wc -l) segments in seg/, $RECLAIMED reclaimed, events in $OUT.jsonl)"
