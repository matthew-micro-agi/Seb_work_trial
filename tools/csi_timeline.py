#!/usr/bin/env python3
"""Put trigger edges, CSI-2 bus events and V4L2 frame timestamps on one timeline.

  tools/csi_timeline.py edges.csv [probe.csv] [frames.jsonl] [--period-ns 33333333]

edges.csv   tools/ptp_extts output (E,<t_ptp>,<chan>,<t_mono>) — the trigger pulses, CLOCK_MONOTONIC
probe.csv   tools/csi_probe output (FS/ON/OFF,<t_mono>,<nb>)   — what the receiver saw on the bus
frames.jsonl kpipe --log output                                 — when V4L2 handed each buffer over

For every event it takes the phase within the trigger period: the time since the most recent edge,
in ms. A phase near 0 means "at the pulse"; a phase near the exposure means "when readout starts".
Prints count, median and range per event class, so one run says where in the period the sensor
starts transmitting, where it stops, and where the DMA completes. Offline tool, runs on the PC.
"""
import bisect, json, statistics, sys


def phases(edges, times, period):
    out = []
    for t in times:
        i = bisect.bisect_right(edges, t) - 1
        if 0 <= i < len(edges) - 1:
            out.append((t - edges[i]) / 1e6)
    return out


def report(name, edges, times, period):
    p = phases(edges, times, period)
    if not p:
        print("%-6s %5d events, none inside the edge series" % (name, len(times)))
        return
    print("%-6s n=%-5d phase after the pulse ms: median %8.3f  min %8.3f  max %8.3f"
          % (name, len(p), statistics.median(p), min(p), max(p)))


def main():
    argv, args, period = sys.argv[1:], [], 33_333_333
    while argv:
        a = argv.pop(0)
        if a == "--period-ns":
            period = int(argv.pop(0))
        elif not a.startswith("--"):
            args.append(a)
    if not args:
        sys.exit(__doc__)
    edges = sorted(int(l.split(",")[3]) for l in open(args[0]) if l.startswith("E,"))
    if len(edges) < 2:
        sys.exit("need at least two edges in %s" % args[0])
    ivals = [(b - a) / 1e6 for a, b in zip(edges, edges[1:])]
    print("edges  n=%-5d interval ms: median %.4f  min %.4f  max %.4f"
          % (len(edges), statistics.median(ivals), min(ivals), max(ivals)))
    for path in args[1:]:
        if path.endswith(".jsonl"):
            ts = [json.loads(l)["ts_ns"] for l in open(path) if '"frame"' in l]
            report("frame", edges, ts, period)
        else:
            groups = {}
            for l in open(path):
                p = l.strip().split(",")
                if len(p) >= 2 and p[0] in ("FS", "SOF", "ON", "OFF", "EOF"):
                    groups.setdefault(p[0], []).append(int(p[1]))
            for k in ("FS", "SOF", "ON", "OFF", "EOF"):
                if k in groups:
                    report(k, edges, groups[k], period)


if __name__ == "__main__":
    main()
