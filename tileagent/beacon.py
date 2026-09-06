"""The beacon: the trigger source's K <-> time log, and the relay that carries it to a recorder.

Two halves, both optional and independent (PLAN_K_MATCHING.md §4, M7).

`TriggerSource` is the source side. It counts the pulses the board *receives* on the CPTS external
timestamp input — the same edges the recorder counts, so no protocol is needed for the two to agree —
and appends one line per second to `<ring>/trigger.log`, served as `GET /v1/trigger/log`:

    {"epoch":2,"k":1188000,"ptp_ns":"5684558371977","utc_ns":"1757152811123456789"}

It lives in the agent rather than in the recorder because that is the whole point: the recorder
restarts and the count must not. Its epoch advances when the pulse train stops for a second (the rule
`kmatch::CapturedEdges` uses, so source and tile find the same boundary) and when the source itself
restarts, because then it genuinely does not know how many pulses it missed. The epoch is seeded one
past the highest in the existing log, so a number is never reused on this source.

`BeaconRelay` is the consumer side. It polls the source's `/v1/trigger/log` over HTTP once a second —
the tail only, with a Range request — and writes the newest line to the running recorder's stdin as

    beacon EPOCH K PTP_NS

The recorder converts the PTP time to its own clock and hands it to `kmatch::CapturedEdges::beacon()`,
which corrects K and logs it. On this bench one board is both source and consumer, and the poll is a
real request to its own HTTP port.
"""
import fcntl
import json
import os
import select
import struct
import threading
import time
import urllib.error
import urllib.request

DEV = "/dev/ptp0"
CHANNEL = 0
EPOCH_GAP_NS = 1_000_000_000        # idle at least this long: the next pulse is K = 0 of a new epoch
REARM_S = 2.0                       # no event for this long: re-issue the request (see OPEN.md §8)
LOG_MAX_LINES = 21_600              # 6 h at 1 Hz, about 1.9 MB; trimmed to half when exceeded
LOG_KEEP_LINES = 10_800

# struct ptp_extts_request { unsigned index, flags; unsigned rsv[2]; }
_EXTTS_REQUEST = struct.Struct("IIII")
# struct ptp_extts_event { struct ptp_clock_time t; unsigned index, flags; unsigned rsv[2]; }
_EXTTS_EVENT = struct.Struct("qIIIIII")
_PTP_EXTTS_REQUEST = (1 << 30) | (_EXTTS_REQUEST.size << 16) | (0x3D << 8) | 2
_PTP_EXTTS_REQUEST2 = (1 << 30) | (_EXTTS_REQUEST.size << 16) | (0x3D << 8) | 11
_PTP_ENABLE_FEATURE, _PTP_RISING_EDGE = 1 << 0, 1 << 1


def _last_line(path, tail=4096):
    """The newest complete line of a file, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().splitlines()
    except OSError:
        return None
    return lines[-1].decode("utf-8", "replace") if lines else None


def parse_line(line):
    """(epoch, k, ptp_ns) from one trigger-log line, or None if it is not one."""
    try:
        d = json.loads(line)
        return int(d["epoch"]), int(d["k"]), int(d["ptp_ns"])
    except (ValueError, TypeError, KeyError):
        return None


class TriggerSource:
    """Counts received trigger pulses and writes the trigger log. One thread, started by the Agent."""

    def __init__(self, path, dev=DEV, channel=CHANNEL, say=print):
        self.path = path
        self.dev, self.channel = dev, channel
        self.say = say
        self.epoch, self.k, self.t_ptp = 0, -1, 0
        self._written = None                  # (epoch, k) of the newest line in the file
        self.pulses = 0                       # since this source started, across epochs
        self.ptp_minus_mono = 0               # kept fresh by the Agent's once-a-second measurement
        self.running = False
        self._stop = threading.Event()
        self._th = None

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────────────
    def start(self):
        """True when the extts channel opened; false on a machine without one (the PC)."""
        try:
            fd = os.open(self.dev, os.O_RDWR)
        except OSError:
            return False
        if not self._request(fd, True):
            os.close(fd)
            return False
        self.epoch = self._seed_epoch()
        self.running = True
        self._th = threading.Thread(target=self._run, args=(fd,), daemon=True)
        self._th.start()
        return True

    def stop(self):
        self._stop.set()
        if self._th is not None:
            self._th.join(2.0)

    def _seed_epoch(self):
        """One past the newest epoch in the existing log: this source has lost count over its downtime."""
        p = parse_line(_last_line(self.path) or "")
        return (p[0] + 1) if p else 1

    def _request(self, fd, on):
        req = _EXTTS_REQUEST.pack(self.channel, (_PTP_ENABLE_FEATURE | _PTP_RISING_EDGE) if on else 0, 0, 0)
        for nr in (_PTP_EXTTS_REQUEST2, _PTP_EXTTS_REQUEST):
            try:
                fcntl.ioctl(fd, nr, req)
                return True
            except OSError:
                continue
        return False

    # ── the counting thread ──────────────────────────────────────────────────────────────────────────
    def _run(self, fd):
        try:
            self._drain(fd)
            t_written = 0.0
            t_event = time.monotonic()
            while not self._stop.is_set():
                ready = select.select([fd], [], [], 0.2)[0]
                now = time.monotonic()
                if ready:
                    self._consume(os.read(fd, _EXTTS_EVENT.size * 32))
                    t_event = now
                elif now - t_event > REARM_S:
                    # Another reader closing its fd disables the channel for everyone (OPEN.md §8).
                    self._request(fd, True)
                    t_event = now
                if self.k >= 0 and (self.epoch, self.k) != self._written and now - t_written >= 1.0:
                    self._write_line()
                    t_written = now
        except OSError as e:
            self.say("trigger source: %s" % e)
        finally:
            self.running = False
            os.close(fd)          # never `_request(fd, False)`: the channel is shared

    def _drain(self, fd):
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, _EXTTS_EVENT.size * 32):
                break

    def _consume(self, buf):
        for off in range(0, len(buf) - _EXTTS_EVENT.size + 1, _EXTTS_EVENT.size):
            sec, nsec, _, index = _EXTTS_EVENT.unpack_from(buf, off)[:4]
            if index != self.channel:
                continue
            t = sec * 1_000_000_000 + nsec
            if self.k < 0 or t - self.t_ptp >= EPOCH_GAP_NS:
                self.epoch, self.k = (self.epoch if self.k < 0 else self.epoch + 1), 0
            else:
                self.k += 1
            self.t_ptp = t
            self.pulses += 1

    # ── the log file ─────────────────────────────────────────────────────────────────────────────────
    def _write_line(self):
        line = json.dumps({"epoch": self.epoch, "k": self.k, "ptp_ns": str(self.t_ptp),
                           "utc_ns": self._utc_ns()}, separators=(",", ":"))
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
            self._written = (self.epoch, self.k)
            self._trim()
        except OSError as e:
            self.say("trigger log: %s" % e)

    def _utc_ns(self):
        """The pulse's time on the system's wall clock, or None while that clock is unset."""
        rt = time.time_ns()
        if rt < 1_600_000_000_000_000_000:       # before 2020: no NTP, no grandmaster, no UTC to state
            return None
        return str(self.t_ptp + rt - time.monotonic_ns() - self.ptp_minus_mono)

    def _trim(self):
        """Keep the log bounded; it shares the ring's budget. Rewrite in place, tail preserved."""
        try:
            if os.path.getsize(self.path) < LOG_KEEP_LINES * 64:
                return
            with open(self.path) as f:
                lines = f.readlines()
            if len(lines) <= LOG_MAX_LINES:
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines[-LOG_KEEP_LINES:])
            os.replace(tmp, self.path)
        except OSError as e:
            self.say("trigger log trim: %s" % e)


class BeaconRelay:
    """Polls a source's /v1/trigger/log and writes each new line to the recorder's stdin."""

    def __init__(self, url, recorder, say=print):
        self.url = url
        self.recorder = recorder
        self.say = say
        self.sent = 0
        self.errors = 0
        self.last = None                  # (epoch, k) most recently forwarded
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._th.start()

    def stop(self):
        self._stop.set()

    def fetch(self):
        """The newest complete line of the source's log, or None. Tail only: the log is megabytes."""
        req = urllib.request.Request(self.url, headers={"Range": "bytes=-4096"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as r:
                body = r.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.errors += 1
            if self.errors in (1, 60) or self.errors % 600 == 0:
                self.say("beacon relay: %s (%d errors)" % (e, self.errors))
            return None
        lines = body.decode("utf-8", "replace").splitlines()
        return lines[-1] if lines else None

    def poll_once(self):
        """One fetch and, if it names a pulse we have not forwarded, one line on the recorder's stdin."""
        p = parse_line(self.fetch() or "")
        if p is None or (p[0], p[1]) == self.last:
            return False
        if not self.recorder.send("beacon %d %d %d" % p):
            return False
        self.last = (p[0], p[1])
        self.sent += 1
        return True

    def _run(self):
        while not self._stop.wait(1.0):
            self.poll_once()
