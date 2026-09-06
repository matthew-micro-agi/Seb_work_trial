"""tile-agent command line.

    python3 -m tileagent --ring /ring --port 8080 --budget 26G                       # the board (systemd unit in board/)
    python3 -m tileagent --ring /dev/shm/ring-fixture --port 8080 --no-recorder      # the PC, idle, serving a fixture ring
    python3 -m tileagent --ring /dev/shm/ring --port 8080 --budget 200M --recorder segfile/build/kpipe-synth
"""
import argparse
import signal
import sys
import threading

from .agent import Agent
from .http import serve


def size(s):
    """'200M', '26G', '3000000' -> bytes."""
    s = s.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(s[-1:], 1)
    return int(float(s[:-1] if mult != 1 else s) * mult)


def main(argv=None):
    p = argparse.ArgumentParser(prog="tile-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ring", required=True, help="ring directory (/ring on the board, any directory on the PC)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--budget", type=size, default=0, help="bytes the ring may occupy, e.g. 26G, 200M; 0 = never reclaim")
    p.add_argument("--lock-budget", type=size, default=0, help="cap on locked bytes; default 40%% of --budget")
    p.add_argument("--segment-s", type=int, default=4)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--width", type=int, default=1456)
    p.add_argument("--height", type=int, default=1088)
    p.add_argument("--exposure", type=int, default=1104, help="IMX296 exposure lines")
    p.add_argument("--gain", type=int, default=0)
    p.add_argument("--bitrate", type=int, default=6_000_000)
    p.add_argument("--recorder", nargs="+", help="recorder command (default pipeline/kpipe.sh); segfile/build/kpipe-synth on the PC")
    p.add_argument("--no-recorder", action="store_true", help="serve the ring only; `recording` is not declared")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--proxy", dest="proxy", action="store_true", default=None,
                   help="declare `live` and run the recorder's proxy substream (default: on unless --recorder names another recorder)")
    g.add_argument("--no-proxy", dest="proxy", action="store_false", help="never declare `live`")
    p.add_argument("--proxy-sock", help="the recorder's proxy socket (default /tmp/tile-live-<ring>.sock)")
    p.add_argument("--proxy-width", type=int, default=728)
    p.add_argument("--proxy-height", type=int, default=544)
    p.add_argument("--proxy-fps", type=int, default=15)
    p.add_argument("--proxy-bitrate", type=int, default=1_000_000)
    p.add_argument("--trigger-sh", help="tools/trigger.sh; trigger.local is declared when it finds the pwmchip")
    p.add_argument("--ptp", default="/dev/ptp0")
    p.add_argument("--beacon-src", help="URL of the trigger source's /v1/trigger/log; the newest line is "
                                        "relayed to the recorder once a second. On the bench this node's own")
    p.add_argument("--no-trigger-log", action="store_true",
                   help="do not count trigger pulses or write <ring>/trigger.log (trigger.log is then declared "
                        "only if the file is already there)")
    p.add_argument("--hw", default=None, help="Node.hw; default from /proc/device-tree/model, else 'pc'")
    p.add_argument("--node-id", default=None, help="Node.node_id; default the eth0 MAC")
    p.add_argument("--verbose", action="store_true", help="log every request")
    a = p.parse_args(argv)
    if a.hw is None:
        try:
            with open("/proc/device-tree/model") as f:
                a.hw = "j722s-evm" if "J722S" in f.read() else "pc"
        except OSError:
            a.hw = "pc"

    say = lambda m: (sys.stderr.write(m + "\n"), sys.stderr.flush())  # noqa: E731
    agent = Agent(a, say)
    srv = serve(agent, a.bind, a.port, a.verbose)
    say("tile-agent: listening on %s:%d" % (a.bind, a.port))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    stop.wait()
    say("tile-agent: stopping")
    agent.shutdown()
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
