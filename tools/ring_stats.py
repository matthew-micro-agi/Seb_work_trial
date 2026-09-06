"""The R2 numbers from a recorder event log (kpipe --ring stdout, or a tile-agent session log):

    python3 tools/ring_stats.py /ring/logs/<session>.jsonl

segments closed (unique, contiguous), frames per segment, fdatasync time per close (p50, p99, max),
reclaim time per victim, bytes/s written, drops.
"""
import json
import statistics
import sys


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def main(path):
    ev = []
    with open(path) as f:
        for line in f:
            try:
                ev.append(json.loads(line))
            except ValueError:
                pass
    closed = [e for e in ev if e.get("ev") == "segment_closed"]
    rec = [e for e in ev if e.get("ev") == "reclaimed"]
    st = [e for e in ev if e.get("ev") == "status" and e.get("state") == "running"]
    if not closed:
        print("no segment_closed events in", path)
        return 1
    segnos = [e["segno"] for e in closed]
    print("segments closed %d, unique %d, contiguous %s, partial %d" % (
        len(closed), len(set(segnos)), segnos == list(range(segnos[0], segnos[0] + len(segnos))), sum(1 for e in closed if e.get("partial"))))
    frames = [e["frames"] for e in closed if "frames" in e]
    if frames:
        print("frames per segment: %s (last %d)" % (sorted(set(frames[:-1])) or frames, frames[-1]))
    fs = [e["fsync_us"] for e in closed if "fsync_us" in e]
    if fs:
        print("fdatasync per close: p50 %d us, p99 %d us, max %d us" % (pct(fs, 0.5), pct(fs, 0.99), max(fs)))
    if rec:
        us = [e["us"] for e in rec if "us" in e]
        print("reclaimed %d victims: p50 %d us, p99 %d us" % (len(rec), pct(us, 0.5), pct(us, 0.99)))
    if st:
        print("bytes/s mean %.0f, fps_in %s, dropped max %d, pre_idr_dropped max %d" % (
            statistics.mean(e.get("bytes_1s", 0) for e in st), sorted(set(e.get("fps_in", 0) for e in st)),
            max(e.get("dropped", 0) for e in st), max(e.get("pre_idr_dropped", 0) for e in st)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) == 2 else 2)
