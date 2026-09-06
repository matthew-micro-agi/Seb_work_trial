"""orch command line (PLAN_ORCH.md §4-§6).

    python3 -m orch nodes|start|stop|trigger|health|watch|segments|lock|unlock|locks|time|targets ...   control
    python3 -m orch fetch|bridge|mcap ...                                                                 data
Tiles from --nodes host:port,... or orch.yaml (orch/orch.yaml is the bench file).
"""
import argparse
import sys

from . import clock


def main(argv=None):
    p = argparse.ArgumentParser(prog="orch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nodes", help="host:port,host:port,... (overrides orch.yaml)")
    p.add_argument("--config", help="orch.yaml path (default ./orch.yaml, then orch/orch.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_, selector=False):
        sp = sub.add_parser(name, help=help_)
        if selector:
            clock.add_selector_args(sp)
        return sp

    add("nodes", "GetNode per tile: id, hw, firmware, capabilities")
    s = add("start", "StartRecording (409 while RUNNING or STOPPING)")
    s.add_argument("--edges", choices=["grid", "ptp"], default="grid")
    s.add_argument("--latency-ns", type=int, default=0,
                   help="trigger edge to frame timestamp; 0 lets the tile use one trigger period (OPEN.md §1)")
    s.add_argument("--fps", type=int); s.add_argument("--exposure-lines", type=int); s.add_argument("--analogue-gain", type=int)
    s.add_argument("--bitrate-bps", type=int); s.add_argument("--segment-s", type=int)
    add("stop", "StopRecording, then wait until IDLE or ERROR")
    s = add("trigger", "SetTrigger (GetTrigger when no setting is given)")
    s.add_argument("--mode", choices=["local", "external"]); s.add_argument("--period-ns", type=int); s.add_argument("--low-ns", type=int)
    g = s.add_mutually_exclusive_group(); g.add_argument("--arm", action="store_true"); g.add_argument("--disarm", action="store_true")
    s = add("health", "GetHealth + GetTelemetry, one row per tile")
    s.add_argument("--watch", action="store_true", help="repeat once a second")
    add("watch", "the health row per tile once a second")
    add("segments", "ListSegments by selector", selector=True)
    s = add("lock", "PutLock by selector (a repeat with a new selector adds)", selector=True)
    s.add_argument("id"); s.add_argument("--reason", default="orch lock"); s.add_argument("--requester", default="orch")
    s = add("unlock", "DeleteLock"); s.add_argument("id")
    add("locks", "ListLocks")
    add("time", "GetTime x5 next to this PC's clock: ptp_locked, boot, offset = ptp - wall")
    s = add("targets", "Prometheus file_sd JSON of the configured tiles"); s.add_argument("--write", metavar="PATH")

    s = add("fetch", "copy footage by selector into --out/<node>/seg/ (PLAN_ORCH.md §5)", selector=True)
    s.add_argument("--out", required=True)
    s.add_argument("--no-lock", action="store_true", help="copy without a temporary lock")
    s.add_argument("--keep-lock", action="store_true", help="leave the lock in place after the copy")
    s.add_argument("--check", action="store_true", help="walk the fetched files: AU count, SEI, K +1")
    s.add_argument("--jobs", type=int, default=4, help="segments in flight per tile")
    s.add_argument("--requester", default="orch@" + __import__("socket").gethostname())
    s = add("bridge", "Foxglove WebSocket server: /tile/<id>/health, telemetry, link, video, frame")
    s.add_argument("--port", type=int, default=8765); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--replay-segment", action="store_true", help="tiles without `live`: publish the newest segment's AUs at their frame rate")
    s = add("mcap", "fetched directory (--out/<node>) -> MCAP for Foxglove")
    s.add_argument("dir"); s.add_argument("-o", "--output", help="default <dir>/<node>.mcap")

    a = p.parse_args(argv)
    if a.cmd == "fetch":
        from .data import fetch
        return fetch.main(a)
    if a.cmd in ("bridge", "mcap"):
        from .data import foxglove
        return getattr(foxglove, a.cmd)(a)
    from . import control
    return getattr(control, "time_" if a.cmd == "time" else a.cmd)(a)


if __name__ == "__main__":
    sys.exit(main())
