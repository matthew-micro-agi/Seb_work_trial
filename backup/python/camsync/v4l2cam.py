"""V4L2 frame source: a real camera in, camsync.frame records out.

  python3 -m camsync.v4l2cam --device /dev/video0 --width 1456 --height 1088 --fourcc Y10 [--strobe-line 42] | binder

The sensor counter S handed to the binder is, in order of preference:
  1. the number of exposure-return (strobe) pulses seen when the frame was delivered
     (--strobe-line); for a sensor without an embedded frame counter, such as the IMX296,
     this hardware pulse count is the exposure count;
  2. the V4L2 sequence number, which counts delivered buffers only, so a lost frame then
     shows up as a sensor miss rather than a delivery loss.

Pixels become NV12 without an ISP: luma only, chroma grey. YUYV keeps the Y bytes, GREY
passes through, 10-bit formats in 16-bit containers shift down (needs numpy), NV12 passes
through. Enough to prove sync and indexing; image quality is the ISP's job later.
"""
import argparse
import fcntl
import mmap
import struct
import sys
import threading

from . import gpio
from .frame import write_frame

QUERYCAP, S_FMT, REQBUFS, QUERYBUF, QBUF, DQBUF, STREAMON = (
    0x80685600, 0xC0D05605, 0xC0145608, 0xC0585609, 0xC058560F, 0xC0585611, 0x40045612)
BUF_TYPE_CAPTURE, MEMORY_MMAP, N_BUFFERS = 1, 1, 8
TEN_BIT = {"Y10 ", "RG10", "GR10", "BA10", "GB10"}


def fourcc(code):
    return int.from_bytes(code.ljust(4).encode(), "little")


def to_nv12(data, width, height, code):
    n = width * height
    if code == "NV12":
        return bytes(data[:n * 3 // 2])
    if code == "YUYV":
        y = bytes(data[0:2 * n:2])
    elif code == "GREY":
        y = bytes(data[:n])
    elif code in TEN_BIT:
        import numpy
        y = (numpy.frombuffer(data, "<u2", n) >> 2).astype("u1").tobytes()
    else:
        raise ValueError("no NV12 conversion for %r" % code)
    return y + b"\x80" * (n // 2)


class Camera:
    def __init__(self, device, width, height, code):
        self.fd = open(device, "rb+", buffering=0)
        fmt = bytearray(208)                                   # struct v4l2_format
        struct.pack_into("<I", fmt, 0, BUF_TYPE_CAPTURE)
        struct.pack_into("<III", fmt, 8, width, height, fourcc(code))
        fmt = fcntl.ioctl(self.fd, S_FMT, bytes(fmt))
        self.width, self.height, got = struct.unpack_from("<III", fmt, 8)
        if (self.width, self.height, got) != (width, height, fourcc(code)):
            raise ValueError("driver gave %dx%d %s" % (self.width, self.height, got.to_bytes(4, "little")))
        req = fcntl.ioctl(self.fd, REQBUFS, struct.pack("<IIIIB3x", N_BUFFERS, BUF_TYPE_CAPTURE, MEMORY_MMAP, 0, 0))
        self.maps = []
        for i in range(struct.unpack_from("<I", req)[0]):
            b = fcntl.ioctl(self.fd, QUERYBUF, self._buffer(i))
            length, offset = struct.unpack_from("<I", b, 72)[0], struct.unpack_from("<I", b, 64)[0]
            self.maps.append(mmap.mmap(self.fd.fileno(), length, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset))
            fcntl.ioctl(self.fd, QBUF, self._buffer(i))
        fcntl.ioctl(self.fd, STREAMON, struct.pack("<i", BUF_TYPE_CAPTURE))

    @staticmethod
    def _buffer(index):
        b = bytearray(88)                                      # struct v4l2_buffer
        struct.pack_into("<II", b, 0, index, BUF_TYPE_CAPTURE)
        struct.pack_into("<I", b, 60, MEMORY_MMAP)
        return bytes(b)

    def frames(self):
        """Yield (sequence, ts_ns, memoryview of the frame bytes); the view is valid until the next frame."""
        while True:
            b = fcntl.ioctl(self.fd, DQBUF, self._buffer(0))
            index, _, used = struct.unpack_from("<III", b, 0)
            sec, usec = struct.unpack_from("<qq", b, 24)
            seq = struct.unpack_from("<I", b, 56)[0]
            yield seq, sec * 1_000_000_000 + usec * 1000, self.maps[index][:used]
            fcntl.ioctl(self.fd, QBUF, self._buffer(index))


def run(args):
    out = sys.stdout.buffer
    strobes = [0]
    if args.strobe_line is not None:
        def count():
            for _ in gpio.EdgeListener(args.gpio_chip, args.strobe_line, "v4l2cam-strobe").events():
                strobes[0] += 1
        threading.Thread(target=count, daemon=True).start()
    cam = Camera(args.device, args.width, args.height, args.fourcc)
    for n, (seq, ts_ns, data) in enumerate(cam.frames()):
        if args.frames and n >= args.frames:
            break
        s = strobes[0] if args.strobe_line is not None else seq
        write_frame(out, seq, ts_ns, cam.width, cam.height, s & 0xFFFF, to_nv12(data, cam.width, cam.height, args.fourcc))
        out.flush()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--width", type=int, default=1456)
    p.add_argument("--height", type=int, default=1088)
    p.add_argument("--fourcc", default="Y10 ", help="V4L2 pixel format: Y10, RG10, GREY, YUYV, NV12")
    p.add_argument("--gpio-chip", default="600000.gpio")
    p.add_argument("--strobe-line", type=int, default=None, help="exposure-return input (GPIO0_42 = J28 pin 22)")
    p.add_argument("--frames", type=int, default=0, help="stop after this many frames (0 = run forever)")
    args = p.parse_args()
    args.fourcc = args.fourcc.ljust(4)
    run(args)


if __name__ == "__main__":
    main()
