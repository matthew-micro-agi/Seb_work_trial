#!/usr/bin/env python3
"""Live-view numbers: read a tile's `GET /v1/live` for a while and say what came out.

  tools/live_stats.py HOST[:PORT] [--seconds 20] [--clients 1]

Per client: access units, frame rate, bitrate, keyframe interval, whether every access unit carries the
camsync SEI and how K steps along the stream. Then the delay from the frame's own timestamp on the tile to
the moment its encoded bytes reached this laptop, using the `GetTime` calibration `orch fetch` uses.

That delay is the pipeline: scale, encode, socket, agent, network. It is **not** glass to glass. Add one
trigger period (33.36 ms at 30 fps) for the CSI-2 receiver, which completes a frame's buffer only when the
next frame starts (`OPEN.md` §1), and whatever the viewer takes to decode and paint.

Runs on the PC with the system python3; no venv (it does not touch the foxglove SDK).
"""
import argparse
import os
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sei", "python"))
import sei  # noqa: E402
from orch import clock  # noqa: E402
from orch.client import Client  # noqa: E402


def stream(client, seconds, out):
    """[(arrival_ns, au_bytes, nals)] for `seconds` of /v1/live, plus the status.

    An access unit is dated by the arrival of its *last* byte, not by the arrival of the next one. The
    Annex-B splitter can only call an access unit complete once the next one starts, which on this stream
    is a whole frame period later; charging that to the tile would be measuring this tool."""
    st, _, r, conn = client.open("/v1/live")
    out["status"] = st
    if st != 200:
        conn.close()
        return
    aus, buf, deadline = [], b"", time.time() + seconds
    out["t_open"] = time.time_ns()
    out["aus"] = aus            # set now: a stream that dies mid-read still reports what it delivered
    base, arrivals = 0, []      # absolute offset of buf[0]; (absolute end offset, arrival) per chunk
    try:
        while time.time() < deadline:
            chunk = r.read1(65536)   # read1: whatever has arrived, not a full 64 KB — this measures latency
            if not chunk:
                break
            buf += chunk
            arrivals.append((base + len(buf), time.time_ns()))
            parts = sei.split(buf, "h264")
            for begin, end, nals in parts[:-1]:
                target = base + end
                aus.append((next(t for off, t in arrivals if off >= target), buf[begin:end], nals))
            if parts:
                base += parts[-1][0]
                buf = buf[parts[-1][0]:]
            arrivals = [(off, t) for off, t in arrivals if off > base]
    except OSError as e:
        out["error"] = str(e)
    finally:
        conn.close()


def report(name, res, seconds, cal, tile_offset_ns):
    if res.get("status") != 200:
        print("%s: GET /v1/live -> %s" % (name, res.get("status")))
        return
    aus = res.get("aus") or []
    if not aus:
        print("%s: no access units in %g s%s" % (name, seconds, "; " + res["error"] if res.get("error") else ""))
        return
    total = sum(len(a) for _, a, _ in aus)
    recs = [sei.record_of(n, "h264") for _, _, n in aus]
    keys = [i for i, (_, _, n) in enumerate(aus) if any(sei.is_idr(x, "h264") for x in n)]
    ks = [r[1] for r in recs if r]
    steps = {}
    for a, b in zip(ks, ks[1:]):
        steps[b - a] = steps.get(b - a, 0) + 1
    span = (aus[-1][0] - aus[0][0]) / clock.NS or 1e-9
    gaps = [b - a for a, b in zip(keys, keys[1:])]
    delay = []
    for (t_recv, _, _), rec in zip(aus, recs):
        if rec:
            delay.append((t_recv - cal.to_wall(rec[2] + tile_offset_ns)) / 1e6)
    print("%s: first byte %.0f ms after the request; %d access units in %.1f s = %.1f fps, %.0f kbps%s"
          % (name, (aus[0][0] - res["t_open"]) / 1e6, len(aus), span, (len(aus) - 1) / span, total * 8 / 1000 / span,
             "  [stream ended: %s]" % res["error"] if res.get("error") else ""))
    print("   keyframes %d, every %s access units; SEI on %d of %d; K %d..%d, steps %s"
          % (len(keys), sorted(set(gaps)) or "n/a", sum(1 for r in recs if r), len(recs),
             ks[0] if ks else -1, ks[-1] if ks else -1, dict(sorted(steps.items()))))
    if delay:
        print("   frame timestamp -> bytes here: mean %.1f ms, min %.1f, max %.1f (add one trigger period"
              " for the receiver, OPEN.md §1)" % (statistics.mean(delay), min(delay), max(delay)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--clients", type=int, default=1)
    a = p.parse_args()

    c = Client(a.host, timeout=max(30, a.seconds + 10))
    node = c.rpc("GetNode")
    if "live" not in node.capabilities:
        sys.exit("%s does not declare `live`: capabilities %s" % (a.host, ",".join(node.capabilities)))
    cal = clock.calibrate(c.time_samples())
    tile_offset = c.rpc("GetTime").ptp_minus_mono_ns          # tile CLOCK_MONOTONIC -> the tile's PTP clock
    cfg = c.rpc("GetConfig").proxy
    print("%s: proxy %dx%d @ %d fps, %d bps; clock offset %.3f s, rtt %.1f ms"
          % (a.host, cfg.width, cfg.height, cfg.fps, cfg.bitrate_bps, cal.offset_ns / clock.NS, cal.rtt_ns / 1e6))

    results = [{} for _ in range(a.clients)]
    clients = [Client(a.host, timeout=max(30, a.seconds + 10)) for _ in range(a.clients)]
    ts = [threading.Thread(target=stream, args=(clients[i], a.seconds, results[i])) for i in range(a.clients)]
    for t in ts:
        t.start()
    time.sleep(min(2.0, a.seconds / 2))
    watching = c.rpc("GetRecording").live_clients
    for t in ts:
        t.join()
    for i, res in enumerate(results):
        report("client %d" % i, res, a.seconds, cal, tile_offset)
    print("live_clients while watching: %d (asked for %d)" % (watching, a.clients))


if __name__ == "__main__":
    main()
