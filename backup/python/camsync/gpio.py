"""GPIO character device (uAPI v2) with the standard library only.

Struct layouts and ioctl numbers were checked against linux/gpio.h (sizes 68 / 592 / 16 / 48).
Edge timestamps are CLOCK_MONOTONIC, taken in the kernel interrupt handler.

CLI, doubles as the trigger-jitter measurement:
  python3 -m camsync.gpio monitor 600000.gpio 33 [--count 10000]
  python3 -m camsync.gpio pulse   600000.gpio 41 [--hz 30]
"""
import argparse
import fcntl
import glob
import os
import statistics
import struct
import time

GET_CHIPINFO = 0x8044B401
GET_LINE = 0xC250B407
SET_VALUES = 0xC010B40F
F_INPUT, F_OUTPUT, F_RISING = 1 << 2, 1 << 3, 1 << 4

_CHIPINFO = struct.Struct("32s32sI")
_EVENT = struct.Struct("<QIIII24x")  # timestamp_ns, id, offset, seqno, line_seqno
_REQ_SIZE = 592


def find_chip(label):
    for path in sorted(glob.glob("/dev/gpiochip*")):
        fd = os.open(path, os.O_RDONLY)
        try:
            _, lbl, _ = _CHIPINFO.unpack(fcntl.ioctl(fd, GET_CHIPINFO, bytes(_CHIPINFO.size)))
        finally:
            os.close(fd)
        if lbl.rstrip(b"\0").decode() == label:
            return path
    raise FileNotFoundError("no gpiochip labelled %s" % label)


def request_line(chip_label, line, flags, consumer, event_buffer=128):
    req = bytearray(_REQ_SIZE)
    struct.pack_into("<I", req, 0, line)                     # offsets[0]
    struct.pack_into("32s", req, 256, consumer.encode())     # consumer
    struct.pack_into("<Q", req, 288, flags)                  # config.flags
    struct.pack_into("<II", req, 560, 1, event_buffer)       # num_lines, event_buffer_size
    fd = os.open(find_chip(chip_label), os.O_RDONLY)
    try:
        req = fcntl.ioctl(fd, GET_LINE, bytes(req))
    finally:
        os.close(fd)
    return struct.unpack_from("<i", req, 588)[0]


class EdgeListener:
    """Rising edges on one line; events() yields kernel timestamps. Lost events are counted
    from the line sequence number the kernel attaches (the 128-event buffer overflowed)."""

    def __init__(self, chip_label, line, consumer):
        self.fd = request_line(chip_label, line, F_INPUT | F_RISING, consumer)
        self.lost = 0
        self._seqno = 0

    def events(self):
        while True:
            data = os.read(self.fd, _EVENT.size * 32)
            for off in range(0, len(data), _EVENT.size):
                ts_ns, _, _, _, seqno = _EVENT.unpack_from(data, off)
                self.lost += seqno - self._seqno - 1
                self._seqno = seqno
                yield ts_ns


class Output:
    def __init__(self, chip_label, line, consumer):
        self.fd = request_line(chip_label, line, F_OUTPUT, consumer)

    def set(self, value):
        fcntl.ioctl(self.fd, SET_VALUES, struct.pack("<QQ", value & 1, 1))

    def pulse(self):
        self.set(1)
        self.set(0)


def _monitor(args):
    prev = None
    intervals = []
    listener = EdgeListener(args.chip, args.line, "camsync-monitor")
    for ts_ns in listener.events():
        if prev is not None:
            intervals.append((ts_ns - prev) / 1e3)
        prev = ts_ns
        if len(intervals) >= args.count:
            break
    print("edges %d  lost %d  interval us: min %.1f  mean %.1f  max %.1f  stdev %.2f  p99 %.1f"
          % (len(intervals) + 1, listener.lost, min(intervals), statistics.fmean(intervals), max(intervals),
             statistics.pstdev(intervals), sorted(intervals)[int(0.99 * (len(intervals) - 1))]))


def _pulse(args):
    out = Output(args.chip, args.line, "camsync-pulse")
    period = 1.0 / args.hz
    while True:
        out.pulse()
        time.sleep(period)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("monitor")
    m.add_argument("chip")
    m.add_argument("line", type=int)
    m.add_argument("--count", type=int, default=1000)
    m.set_defaults(fn=_monitor)
    q = sub.add_parser("pulse")
    q.add_argument("chip")
    q.add_argument("line", type=int)
    q.add_argument("--hz", type=float, default=30)
    q.set_defaults(fn=_pulse)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
