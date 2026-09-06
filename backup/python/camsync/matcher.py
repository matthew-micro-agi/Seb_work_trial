"""Trigger-index matching and gap detection.

    K = (S - S0) + M

  S   sensor frame counter read from the frame's embedded data (16-bit, unwrapped here)
  S0  the counter value of the first frame after the trigger was (re)enabled
  M   edges the sensor missed so far: the edge fired, no exposure happened

Per delivered frame, with dS = S - S_prev and n = round(dt / period):
  dS - 1 > 0   frames the sensor exposed that were lost afterwards   -> "delivery_lost"
  n  - dS > 0  edges the sensor did not act on                        -> "sensor_missed", added to M

A frame delayed by more than half a period misreads n by one and leaves K off by
one. The window check compares K's advance against elapsed time once per window
and corrects M, so such an error lasts one window instead of the session.

Edge and strobe events are evidence only: they feed the stall alarm and the
cross-checks, never the per-frame K.

Log events (dicts handed to `log`), the vocabulary the binder writes and the checker reads:
  rearm            new epoch, K restarts at the next frame
  anchor           first frame of the epoch: s0, edges seen so far
  gap              K jumped: from_k..to_k, delivery_lost, sensor_missed, trigger_stalled
  dup              same exposure delivered again
  k_corrected      window check moved K by `by`
  trigger_stall    no edge for two periods (needs tick())
  trigger_resumed  edges are back
  strobe_mismatch  strobes and exposures disagree over a window
"""
from dataclasses import dataclass

FLAG_DUP = 1          # same exposure delivered again; K unchanged
FLAG_GAP_BEFORE = 2   # K advanced by more than one
FLAG_CORRECTED = 4    # window check adjusted K on this frame


@dataclass
class Result:
    k: int
    flags: int = 0
    delivery_lost: int = 0
    sensor_missed: int = 0


class Matcher:
    def __init__(self, period_ns, check_ns=60_000_000_000, log=lambda ev: None):
        self.period_ns = period_ns
        self.check_ns = check_ns
        self.log = log
        self.epoch = 0
        self.edges = 0
        self.last_edge_ns = None
        self.stalled = False
        self.stall_seen = False
        self.strobes = 0
        self.rearm()

    def rearm(self):
        """The trigger was (re)enabled: the next frame anchors K = 0 in a new epoch."""
        self.epoch += 1
        self.s0 = None
        self.s_raw = None
        self.s = 0
        self.m = 0
        self.k = -1
        self.ts_prev = None
        self.check_ts = None
        self.check_k = self.check_s = self.check_strobes = 0
        self.log({"ev": "rearm", "epoch": self.epoch})

    def edge(self, ts_ns):
        self.edges += 1
        self.last_edge_ns = ts_ns
        if self.stalled:
            self.stalled = False
            self.log({"ev": "trigger_resumed", "ts_ns": ts_ns, "edges": self.edges})

    def strobe(self, ts_ns):
        self.strobes += 1

    def tick(self, now_ns):
        """Call periodically when an edge listener is attached."""
        if self.last_edge_ns is None or self.stalled:
            return
        if now_ns - self.last_edge_ns > 2 * self.period_ns:
            self.stalled = self.stall_seen = True
            self.log({"ev": "trigger_stall", "ts_ns": now_ns, "last_edge_ns": self.last_edge_ns})

    def frame(self, ts_ns, sensor_count):
        if self.s0 is None:
            self.s_raw = self.s = self.s0 = self.check_s = sensor_count
            self.k, self.ts_prev, self.check_ts = 0, ts_ns, ts_ns
            self.log({"ev": "anchor", "epoch": self.epoch, "s0": sensor_count,
                      "ts_ns": ts_ns, "edges": self.edges, "last_edge_ns": self.last_edge_ns})
            return Result(0)
        ds = self._unwrap(sensor_count)
        if ds <= 0:
            self.log({"ev": "dup", "epoch": self.epoch, "k": self.k, "ts_ns": ts_ns})
            return Result(self.k, FLAG_DUP)
        n = max(1, round((ts_ns - self.ts_prev) / self.period_ns))
        delivery_lost = ds - 1
        sensor_missed = max(0, n - ds)
        self.m += sensor_missed
        k_prev, self.k = self.k, (self.s - self.s0) + self.m
        self.ts_prev = ts_ns
        flags = 0
        if self.k != k_prev + 1:
            flags |= FLAG_GAP_BEFORE
            self.log({"ev": "gap", "epoch": self.epoch, "from_k": k_prev + 1, "to_k": self.k - 1,
                      "delivery_lost": delivery_lost, "sensor_missed": sensor_missed,
                      "trigger_stalled": self.stall_seen, "ts_ns": ts_ns})
        self.stall_seen = False
        flags |= self._window_check(ts_ns)
        return Result(self.k, flags, delivery_lost, sensor_missed)

    def _unwrap(self, raw):
        """Track the 16-bit counter across wrap; return the signed step since the previous frame."""
        step = (raw - self.s_raw) & 0xFFFF
        if step >= 0x8000:
            step -= 0x10000
        self.s_raw = raw
        self.s += step
        return step

    def _window_check(self, ts_ns):
        if ts_ns - self.check_ts < self.check_ns:
            return 0
        expected = round((ts_ns - self.check_ts) / self.period_ns)
        actual = self.k - self.check_k
        flags = 0
        if actual != expected:
            self.m += expected - actual
            self.k += expected - actual
            flags = FLAG_CORRECTED
            self.log({"ev": "k_corrected", "epoch": self.epoch, "by": expected - actual,
                      "k": self.k, "ts_ns": ts_ns})
        if self.strobes:
            exposures = self.s - self.check_s
            if abs((self.strobes - self.check_strobes) - exposures) > 1:
                self.log({"ev": "strobe_mismatch", "strobes": self.strobes - self.check_strobes,
                          "exposures": exposures, "ts_ns": ts_ns})
        self.check_ts, self.check_k, self.check_s, self.check_strobes = ts_ns, self.k, self.s, self.strobes
        return flags
