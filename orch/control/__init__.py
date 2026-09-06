"""orch control: the small commands of PLAN_ORCH.md §4, one exchange each, run against every configured
tile in parallel. Portable to an MCU: the generated classes, client.py and clock.py, nothing else."""
import json
import sys
import time

from .. import clock, pb
from ..client import fanout, load_nodes

STOP_WAIT_S = 10


def _hosts(args):
    return load_nodes(args.nodes, args.config)


def _state(rs):
    return pb.RecordingStatus.State.Name(rs.state)


def _mb(n):
    return "%.0fM" % (n / 1e6)


def nodes(args):
    def one(c):
        n = c.rpc("GetNode")
        return "%s hw=%s fw=%s api=%s caps=%s" % (n.node_id, n.hw, n.firmware, n.api_version, ",".join(n.capabilities))
    return fanout(_hosts(args), one)


def start(args):
    req = pb.StartRecordingRequest(edges=getattr(pb.StartRecordingRequest, args.edges.upper()), latency_ns=args.latency_ns)
    for name in ("fps", "exposure_lines", "analogue_gain", "bitrate_bps", "segment_s"):
        if getattr(args, name) is not None:
            setattr(req, name, getattr(args, name))

    def one(c):
        rs = c.rpc("StartRecording", req)
        return "%s session=%s" % (_state(rs), rs.session)
    return fanout(_hosts(args), one)


def stop(args):
    def one(c):
        rs = c.rpc("StopRecording")
        deadline = time.time() + STOP_WAIT_S
        while rs.state == pb.RecordingStatus.STOPPING and time.time() < deadline:
            time.sleep(0.5)
            rs = c.rpc("GetRecording")
        return _state(rs) + (" (still stopping after %d s)" % STOP_WAIT_S if rs.state == pb.RecordingStatus.STOPPING else "")
    return fanout(_hosts(args), one)


def trigger(args):
    s = pb.TriggerSettings()
    if args.mode:
        s.mode = getattr(pb.TriggerSettings, args.mode.upper())
    if args.arm or args.disarm:
        s.armed = bool(args.arm)
    if args.period_ns:
        s.period_ns = args.period_ns
    if args.low_ns:
        s.low_ns = args.low_ns
    setting = bool(args.mode or args.arm or args.disarm or args.period_ns or args.low_ns)

    def one(c):
        if not setting:
            return _trigger_line(c.rpc("GetTrigger"))
        req = pb.TriggerSettings()
        req.CopyFrom(s)
        if not (args.arm or args.disarm):
            # `armed` is a proto3 bool with no presence, so an unset one arrives as false and the tile
            # would disarm — stopping the pulse train, and with it the capture — on a bare --period-ns.
            # Every other field keeps its current value on the tile; this one has to be carried over here.
            req.armed = c.rpc("GetTrigger").settings.armed
        return _trigger_line(c.rpc("SetTrigger", req))
    return fanout(_hosts(args), one)


def _trigger_line(t):
    return "%s armed=%s period_ns=%d low_ns=%d epoch=%d k=%d edges_seen=%d" % (
        pb.TriggerSettings.Mode.Name(t.settings.mode), t.settings.armed, t.settings.period_ns, t.settings.low_ns, t.epoch, t.k, t.edges_seen)


def sample(c):
    """(Health, Telemetry): the one pair health, watch and the bridge poll."""
    return c.rpc("GetHealth"), c.rpc("GetTelemetry")


def health_row(h, t, budget=None):
    faults = ",".join(pb.NodeFault.Name(f) for f in h.faults_active) or "-"
    used = _mb(h.ring.used_bytes) + ("/" + _mb(budget) if budget else "")
    return "%-8s fps=%-3d K=%d/%d segs=%d used=%s locked=%s faults=%s" % (
        _state(h.recording), t.fps_in, t.epoch, t.k, h.ring.segments, used, _mb(h.ring.locked_bytes), faults)


def health(args):
    budgets = {}

    def one(c):
        if c.host not in budgets:
            budgets[c.host] = c.rpc("GetConfig").ring.budget_bytes
        h, t = sample(c)
        return health_row(h, t, budgets[c.host])
    if not args.watch:
        return fanout(_hosts(args), one)
    hosts = _hosts(args)
    try:
        while True:
            lines = []
            fanout(hosts, one, say=lines.append)
            sys.stdout.write("\x1b[H\x1b[J" + time.strftime("%H:%M:%S") + "\n" + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def watch(args):
    args.watch = True
    return health(args)


def segments(args):
    cal_needed = clock.selector_kind(args) == "ptp"

    def one(c):
        cal = clock.calibrate(c.time_samples()) if cal_needed else None
        lst = c.rpc("ListSegments", pb.ListSegmentsRequest(select=clock.build_selector(args, cal)))
        out = ["%d segments, %s" % (len(lst.segments), _mb(sum(s.bytes for s in lst.segments)))]
        for s in lst.segments:
            k = "K %d:%d..%s%d" % (s.first.epoch, s.first.k, "" if s.last.epoch == s.first.epoch else "%d:" % s.last.epoch, s.last.k) if s.HasField("first") else "K -"
            out.append("  %6d %-22s %s frames=%d bytes=%d%s%s%s" % (
                s.segno, s.session, k, s.frames, s.bytes, " partial" if s.partial else "",
                " faults=" + ",".join(pb.Fault.Name(f) for f in s.faults) if s.faults else "",
                " locks=" + ",".join(s.locks) if s.locks else ""))
        return out
    return fanout(_hosts(args), one)


def lock(args):
    cal_needed = clock.selector_kind(args) == "ptp"

    def one(c):
        cal = clock.calibrate(c.time_samples()) if cal_needed else None
        r = c.rpc("PutLock", pb.PutLockRequest(id=args.id, select=clock.build_selector(args, cal), reason=args.reason, requester=args.requester))
        return "%s %s: %d segments, %s%s%s" % ("created" if r.created else "extended", r.lock.id, len(r.lock.segnos), _mb(r.lock.bytes),
                                               " added=%s" % list(r.added) if not r.created else "", " missing=%s" % list(r.missing) if r.missing else "")
    return fanout(_hosts(args), one)


def unlock(args):
    def one(c):
        c.rpc("DeleteLock", pb.LockId(id=args.id))
        return "deleted %s" % args.id
    return fanout(_hosts(args), one)


def locks(args):
    def one(c):
        ls = c.rpc("ListLocks").locks
        return ["%d locks" % len(ls)] + ["  %-32s %-16s %d segments %s %s" % (l.id, l.requester, len(l.segnos), _mb(l.bytes), l.reason) for l in ls]
    return fanout(_hosts(args), one)


def time_(args):
    def one(c):
        cal = clock.calibrate(c.time_samples())
        line = "ptp_locked=%s boot=%s offset(ptp-wall)=%.6f s rtt=%.1f ms" % (cal.ptp_locked, cal.boot_id[:8], cal.offset_ns / clock.NS, cal.rtt_ns / 1e6)
        return [line] + ["  warning: " + w for w in cal.warnings()]
    return fanout(_hosts(args), one)


def targets(args):
    """Prometheus file_sd JSON for the configured tiles (orch/prometheus.yml reads targets.json)."""
    doc = [{"targets": _hosts(args), "labels": {"job": "tiles"}}]
    text = json.dumps(doc, indent=2) + "\n"
    if args.write:
        with open(args.write, "w") as f:
            f.write(text)
        print("%s: %d targets" % (args.write, len(doc[0]["targets"])))
    else:
        sys.stdout.write(text)
    return 0
