"""Wall clock <-> PTP (DESIGN_RING_CONTROL.md §8.6, PLAN_ORCH.md §5), and the one selector grammar.
Pure: no I/O. The offset is `ptp - wall`; ptp = wall + offset; wall = ptp - offset."""
import datetime
import re

from . import pb

TAI_UTC_S = 37          # what a locked PTP clock (TAI) is ahead of UTC today
NS = 1_000_000_000


class Calibration:
    def __init__(self, offset_ns, rtt_ns, time_msg):
        self.offset_ns, self.rtt_ns = offset_ns, rtt_ns
        self.ptp_locked, self.boot_id = time_msg.ptp_locked, time_msg.boot_id

    def to_ptp(self, wall_ns):
        return wall_ns + self.offset_ns

    def to_wall(self, ptp_ns):
        return ptp_ns - self.offset_ns

    def warnings(self):
        out = []
        if not self.ptp_locked:
            out.append("PTP free-running on this tile: time selection reaches footage from this boot only (boot %s)" % self.boot_id[:8])
        elif abs(self.offset_ns - TAI_UTC_S * NS) > NS:
            out.append("PTP locked but offset %.3f s is not TAI-UTC (%d s): check the grandmaster or this PC's clock" % (self.offset_ns / NS, TAI_UTC_S))
        return out

    def as_dict(self):
        return {"offset_ns": self.offset_ns, "rtt_ns": self.rtt_ns, "ptp_locked": self.ptp_locked, "boot_id": self.boot_id}


def calibrate(samples):
    """samples: [(wall_before_ns, wall_after_ns, Time)]; the shortest round trip wins, read at its midpoint."""
    t0, t1, msg = min(samples, key=lambda s: s[1] - s[0])
    return Calibration(msg.ptp_ns - (t0 + t1) // 2, t1 - t0, msg)


def local_to_utc_ns(hhmm, date=None):
    """'HH:MM' or 'HH:MM:SS' on `date` (YYYY-MM-DD, default today), in this machine's local zone."""
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", hhmm)
    if not m:
        raise ValueError("time %r: expected HH:MM or HH:MM:SS" % hhmm)
    day = datetime.date.fromisoformat(date) if date else datetime.date.today()
    t = datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))).astimezone()
    return int(t.timestamp()) * NS


def fmt_wall(ns):
    return datetime.datetime.fromtimestamp(ns / NS).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def edges(since_ptp, until_ptp, oldest_ptp, newest_ptp):
    """How the request relates to the footage the tile holds (Health.ring): None when nothing overlaps,
    else {'head_missing_s', 'tail_missing_s'} (0 when covered)."""
    if not oldest_ptp or until_ptp < oldest_ptp or since_ptp > newest_ptp:
        return None
    return {"head_missing_s": max(0, oldest_ptp - since_ptp) / NS, "tail_missing_s": max(0, until_ptp - newest_ptp) / NS}


def same_boot(session, boot_id):
    """True/False for a '<boot_id[:8]>-<ms>' session id; None for 'recovered' (rebuilt index, unknown boot)."""
    if not re.match(r"^[0-9a-f]{8}-\d+$", session):
        return None
    return session[:8] == boot_id[:8]


# ── the selector grammar: --last | --from --to [--date] | --k | --segno ───────────────────────────────
def add_selector_args(p):
    g = p.add_argument_group("selector (exactly one)")
    g.add_argument("--last", type=float, metavar="S", help="footage of the last S seconds")
    g.add_argument("--from", dest="since", metavar="HH:MM[:SS]", help="wall-clock start (local time)")
    g.add_argument("--to", dest="until", metavar="HH:MM[:SS]", help="wall-clock end (local time)")
    g.add_argument("--date", metavar="YYYY-MM-DD", help="date for --from/--to; default today")
    g.add_argument("--k", metavar="EPOCH:FROM:TO", help="trigger index range, inclusive")
    g.add_argument("--segno", metavar="FROM:TO", help="segment numbers, inclusive")


def selector_kind(args):
    kinds = [k for k, v in (("last", args.last), ("ptp", args.since or args.until), ("k", args.k), ("segno", args.segno)) if v]
    if len(kinds) != 1:
        raise SystemExit("selector: give exactly one of --last, --from/--to, --k, --segno")
    if kinds[0] == "ptp" and not (args.since and args.until):
        raise SystemExit("selector: --from and --to go together")
    return kinds[0]


def wall_interval(args):
    """(since_utc_ns, until_utc_ns) of a --from/--to request."""
    a, b = local_to_utc_ns(args.since, args.date), local_to_utc_ns(args.until, args.date)
    if b <= a:
        raise SystemExit("selector: --to must be after --from")
    return a, b


def build_selector(args, cal=None):
    """A tile.v1 Selector from the arguments; the wall-clock arm needs a Calibration."""
    kind = selector_kind(args)
    if kind == "last":
        return pb.Selector(last_s=args.last)
    if kind == "k":
        e, a, b = (int(x) for x in args.k.split(":"))
        return pb.Selector(k=pb.KRange(epoch=e, k_from=a, k_to=b))
    if kind == "segno":
        a, b = (int(x) for x in args.segno.split(":"))
        return pb.Selector(segno=pb.SegnoRange(**{"from": a, "to": b}))
    since, until = wall_interval(args)
    return pb.Selector(ptp=pb.PtpInterval(since_ns=cal.to_ptp(since), until_ns=cal.to_ptp(until)))
