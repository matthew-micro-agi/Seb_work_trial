"""Clocks: CLOCK_MONOTONIC, the PTP hardware clock's offset to it, and the series of offset samples that
converts a segment's frame times to PTP (DESIGN_RING_CONTROL.md §5, one derivation for live and rebuild)."""
import bisect
import fcntl
import os
import struct
import time

_PTP_CLOCK_TIME = struct.Struct("qII")                       # sec, nsec, reserved
_N = 5
# _IOC(dir, '=', nr, size): dir 1 = write, 3 = read/write
_PTP_SYS_OFFSET = (1 << 30) | ((4 + 12 + 51 * 16) << 16) | (0x3D << 8) | 5
_PTP_SYS_OFFSET_EXTENDED = (3 << 30) | ((4 + 12 + 25 * 3 * 16) << 16) | (0x3D << 8) | 9


def mono_ns():
    return time.monotonic_ns()


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _t(buf, off):
    sec, nsec, _ = _PTP_CLOCK_TIME.unpack_from(buf, off)
    return sec * 1_000_000_000 + nsec


class PtpClock:
    """ptp_minus_mono_ns from /dev/ptp0, or None on a machine without one (the PC). Mirrors
    pipeline/src/edgefeed.cpp: PTP_SYS_OFFSET_EXTENDED (fallback PTP_SYS_OFFSET) against CLOCK_REALTIME,
    then realtime - monotonic measured back to back."""

    def __init__(self, dev="/dev/ptp0"):
        self.fd = None
        try:
            self.fd = os.open(dev, os.O_RDWR)
        except OSError:
            pass

    @property
    def present(self):
        return self.fd is not None

    def sample(self):
        if self.fd is None:
            return None
        rt0 = time.time_ns(); mo = time.monotonic_ns(); rt1 = time.time_ns()
        rt_minus_mono = (rt0 + rt1) // 2 - mo
        try:
            buf = bytearray(struct.pack("I3I", _N, 0, 0, 0)) + bytearray(25 * 3 * 16)
            fcntl.ioctl(self.fd, _PTP_SYS_OFFSET_EXTENDED, buf)
            triples = [(_t(buf, 16 + (3 * i) * 16), _t(buf, 16 + (3 * i + 1) * 16), _t(buf, 16 + (3 * i + 2) * 16)) for i in range(_N)]
        except OSError:
            try:
                buf = bytearray(struct.pack("I3I", _N, 0, 0, 0)) + bytearray(51 * 16)
                fcntl.ioctl(self.fd, _PTP_SYS_OFFSET, buf)
                ts = [_t(buf, 16 + i * 16) for i in range(2 * _N + 1)]
                triples = [(ts[2 * i], ts[2 * i + 1], ts[2 * i + 2]) for i in range(_N)]
            except OSError:
                return None
        a, d, b = min(triples, key=lambda t: t[2] - t[0])
        return d - (a + b) // 2 + rt_minus_mono


class OffsetSeries:
    """(t_mono, ptp_minus_mono) samples; at(t) is the newest sample not after t, else the oldest."""

    def __init__(self):
        self.t = []
        self.off = []

    def add(self, t_mono, off):
        i = bisect.bisect_right(self.t, t_mono)
        self.t.insert(i, t_mono)
        self.off.insert(i, off)

    def at(self, t_mono):
        if not self.t:
            return None
        i = bisect.bisect_right(self.t, t_mono)
        return self.off[i - 1] if i > 0 else self.off[0]

    def latest(self):
        return self.off[-1] if self.off else None
