#!/usr/bin/env python3
"""What trigger pulse took each frame, read out of the stored video itself.

Every access unit carries a camsync SEI: epoch, K, the frame's timestamp, the sensor count and the fault
flags. That record is the binding between a picture and the pulse that exposed it, and it is inside the
bitstream, so it survives copying, concatenation and anything that does not re-encode. This is the tool
that shows it.

    tools/frame_index.py FILE...              one line per frame
    tools/frame_index.py --summary FILE...    one line per file
    tools/frame_index.py --gaps FILE...       only the frames that break the chain or carry a fault
    tools/frame_index.py --codec h264 FILE    the proxy substream (GET /v1/live) instead of a segment

Files come from `orch fetch`, from `GET /v1/segments/{segno}/data`, or straight off the tile's ring.
Several files are read as one stream, so a shell glob checks a whole recording:

    tools/frame_index.py --gaps rec/*/seg/*.h265
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sei", "python"))
import sei  # noqa: E402

# K_LOCAL is a standing label, not an event: it marks every frame until a beacon anchors the session, so
# --gaps ignores it and reports the moment it clears (K_CORRECTED) instead.
LABEL_ONLY = 1 << 9
FAULTS = [(1 << 0, "DUPLICATE"), (1 << 1, "DELIVERY_LOST"), (1 << 2, "SENSOR_MISSED"), (1 << 3, "LATE"),
          (1 << 4, "UNMATCHED"), (1 << 5, "EPOCH_CHANGE"), (1 << 6, "SEQ_REGRESSION"), (1 << 7, "EDGE_PENDING"),
          (1 << 8, "K_CORRECTED"), (1 << 9, "K_LOCAL"), (1 << 15, "STAMP_LOST")]


def fault_names(flags):
    return ",".join(n for bit, n in FAULTS if flags & bit) or "-"


def frames(path, codec):
    """(index in file, record or None) for every access unit."""
    with open(path, "rb") as f:
        data = f.read()
    for i, au in enumerate(sei.split(data, codec)):
        nals = au[2] if isinstance(au, tuple) and len(au) == 3 else au
        yield i, sei.record_of(nals, codec)


def expected_step(records):
    """The stream's own K step: 1 for a recording, N for a substream decimated 1-in-N. Taken from the
    data (the commonest delta) rather than assumed, so a decimated proxy is not read as 120 dropped
    frames while a real drop still stands out against its neighbours."""
    deltas = {}
    prev = None
    for epoch, k in records:
        if prev is not None and epoch == prev[0]:
            deltas[k - prev[1]] = deltas.get(k - prev[1], 0) + 1
        prev = (epoch, k)
    return max(deltas, key=deltas.get) if deltas else 1


def main():
    ap = argparse.ArgumentParser(description="the trigger index of every frame, read from the video")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--codec", default="h265", choices=("h265", "h264"), help="h264 for the proxy substream")
    ap.add_argument("--summary", action="store_true", help="one line per file")
    ap.add_argument("--gaps", action="store_true", help="only breaks in the chain and frames with faults")
    a = ap.parse_args()

    # One pass to learn the stream's natural step, then one to report against it.
    seen = []
    for path in a.files:
        seen += [(r[0], r[1]) for _, r in frames(path, a.codec) if r is not None]
    step = expected_step(seen)

    prev = None            # (epoch, k) of the previous frame, across files
    problems = 0
    for path in a.files:
        first = last = None
        n = missing = 0
        faults = 0
        for i, r in frames(path, a.codec):
            n += 1
            if r is None:
                missing += 1
                problems += 1
                if not a.summary:
                    print("%s  au %-5d  NO SEI" % (os.path.basename(path), i))
                continue
            epoch, k, ts, count, flags = r
            faults |= flags
            step_ok = prev is None or (epoch, k) == (prev[0], prev[1] + step)
            notable = flags & ~LABEL_ONLY
            if not step_ok or notable:
                problems += 1
            if first is None:
                first = (epoch, k)
            last = (epoch, k)
            if a.summary:
                pass
            elif not a.gaps or not step_ok or notable:
                note = ""
                if not step_ok and prev is not None:
                    note = "   <-- chain: %d:%d then %d:%d" % (prev[0], prev[1], epoch, k)
                    if prev[0] == epoch and step != 1:
                        note += " (step %d)" % step
                print("%s  au %-5d  epoch %-3d K %-9d  t %d ns  s %-6d  %s%s"
                      % (os.path.basename(path), i, epoch, k, ts, count, fault_names(flags), note))
            prev = (epoch, k)
        if a.summary:
            span = "%d:%d..%d:%d" % (first + last) if first else "no SEI at all"
            print("%-28s %4d frames  K %-22s %s%s"
                  % (os.path.basename(path), n, span, fault_names(faults),
                     "  %d WITHOUT SEI" % missing if missing else ""))
    if a.gaps and not problems:
        print("no chain breaks and no faults in %d file(s)%s"
              % (len(a.files), "" if step == 1 else ", K stepping by %d" % step))
    elif a.gaps:
        print("%d frame(s) of note" % problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
