#!/usr/bin/env python3
"""M2 numbers: frame timestamp minus trigger edge time, from a kpipe log and a ptp_extts capture.

  tools/latency_stats.py rec.jsonl edges.csv [--period-ns 33333333]

edges.csv is the output of tools/ptp_extts (E,<t_ptp>,<chan>,<t_mono> lines). For every frame the
nearest earlier edge within one period is taken; prints count, mean, stdev, min, max of the
difference in microseconds. The mean is the `--latency-ns` for kpipe, the spread is the receiver's
timestamp jitter (PLAN M2). Offline tool, runs on the PC.
"""
import bisect, json, statistics, sys

def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    period = 33_333_333
    if "--period-ns" in sys.argv:
        period = int(sys.argv[sys.argv.index("--period-ns") + 1])
    frames = [json.loads(l)["ts_ns"] for l in open(sys.argv[1]) if '"frame"' in l]
    edges = sorted(int(l.split(",")[3]) for l in open(sys.argv[2]) if l.startswith("E,"))
    d = []
    for t in frames:
        i = bisect.bisect_right(edges, t) - 1
        if i >= 0 and t - edges[i] < period:
            d.append((t - edges[i]) / 1000.0)
    if not d:
        sys.exit("no frame had an edge within one period before it: are both on CLOCK_MONOTONIC?")
    print("frames %d  edges %d  matched %d" % (len(frames), len(edges), len(d)))
    print("latency us: mean %.1f  stdev %.1f  min %.1f  max %.1f" % (statistics.mean(d), statistics.pstdev(d), min(d), max(d)))
    print("-> kpipe --latency-ns %d" % int(statistics.mean(d) * 1000))

if __name__ == "__main__":
    main()
