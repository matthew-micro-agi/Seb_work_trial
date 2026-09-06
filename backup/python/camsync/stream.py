"""Conversion helpers and a pipe-based preview source. The pipe path tops out at 15 fps on the A53s
(the 3 MB uncached V4L2 buffer copy plus pipe traffic exceed one frame period); camsync.gststream
feeds GStreamer directly and holds 30 fps. Kept for the helpers and as a fallback.

Raw Bayer from the IMX296 in, NV12 frames out on stdout, for a GStreamer encoder behind a pipe. Two modes:

  luma   (default) full resolution, 10-bit -> 8-bit, chroma grey. About 7 ms per frame on one A53.
  color  half resolution: each 2x2 Bayer quad becomes one RGB pixel, fixed white balance gains,
         BT.601 to NV12. About the same cost, and it shows colour.

  python3 -m camsync.stream --device /dev/video3 [--color] | gst-launch-1.0 fdsrc ! rawvideoparse ...

Handles the one quirk seen on the bench: the first stream-on after the sensor powered up fails
with -EREMOTEIO on its first register write; the second attempt works. So the sensor is held
powered (runtime PM "on") and stream-on is retried once.
"""
import argparse
import fcntl
import glob
import sys
import time

import numpy

from .v4l2cam import Camera

BAYER_ORDER = {"BG10": "bggr", "RG10": "rggb", "GB10": "gbrg", "BA10": "grbg"}


def hold_sensor_powered():
    for ctl in glob.glob("/sys/bus/i2c/devices/*/name"):
        if open(ctl).read().strip() == "imx296":
            try:
                open(ctl.replace("/name", "/power/control"), "w").write("on")
                time.sleep(0.2)
            except OSError:
                pass


def open_camera(device, width, height, fourcc, retries=2):
    for attempt in range(retries + 1):
        try:
            return Camera(device, width, height, fourcc)
        except OSError as e:
            if attempt == retries:
                raise
            print("stream-on failed (%s), retrying" % e, file=sys.stderr)
            time.sleep(0.5)


def luma_nv12(raw, width, height):
    n = width * height
    y = (numpy.frombuffer(raw, "<u2", n) >> 2).astype("u1")
    return y.tobytes() + b"\x80" * (n // 2)


def color_nv12(raw, width, height, order, gains):
    """2x2 Bayer quad -> one NV12 pixel at half resolution. Integer math (int32), BT.601 limited range."""
    a = numpy.frombuffer(raw, "<u2", width * height).reshape(height, width)
    q = {"bggr": (a[1::2, 1::2], a[0::2, 1::2], a[1::2, 0::2], a[0::2, 0::2]),
         "rggb": (a[0::2, 0::2], a[0::2, 1::2], a[1::2, 0::2], a[1::2, 1::2]),
         "gbrg": (a[1::2, 0::2], a[0::2, 0::2], a[1::2, 1::2], a[0::2, 1::2]),
         "grbg": (a[0::2, 1::2], a[0::2, 0::2], a[1::2, 1::2], a[1::2, 0::2])}[order]
    gr, gg, gb = (int(g * 64) for g in gains)                      # gains in 1/64 steps
    r = q[0].astype(numpy.int32) * gr                                # 10-bit * 64 -> 16-bit scale
    g = (q[1].astype(numpy.int32) + q[2]) * (gg // 2)
    b = q[3].astype(numpy.int32) * gb
    # 8-bit = value >> 8 ; Y = 16 + (66R + 129G + 25B) >> 8 ; U = 128 + (-38R - 74G + 112B) >> 8 ; V = 128 + (112R - 94G - 18B) >> 8
    y = numpy.clip(16 + ((66 * r + 129 * g + 25 * b) >> 16), 16, 235).astype("u1")
    u = 128 + ((-38 * r - 74 * g + 112 * b) >> 16)
    v = 128 + ((112 * r - 94 * g - 18 * b) >> 16)
    u = numpy.clip((u[0::2, 0::2] + u[0::2, 1::2] + u[1::2, 0::2] + u[1::2, 1::2]) >> 2, 16, 240).astype("u1")
    v = numpy.clip((v[0::2, 0::2] + v[0::2, 1::2] + v[1::2, 0::2] + v[1::2, 1::2]) >> 2, 16, 240).astype("u1")
    uv = numpy.empty((u.shape[0], u.shape[1] * 2), "u1"); uv[:, 0::2] = u; uv[:, 1::2] = v
    return y.tobytes() + uv.tobytes()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="/dev/video3")
    p.add_argument("--width", type=int, default=1456)
    p.add_argument("--height", type=int, default=1088)
    p.add_argument("--fourcc", default="BG10", help="V4L2 pixel format of the capture node")
    p.add_argument("--color", action="store_true", help="half-resolution colour instead of full-resolution luma")
    p.add_argument("--gains", default="1.6,1.0,1.4", help="R,G,B white balance gains for --color")
    p.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = forever)")
    p.add_argument("--stats", action="store_true", help="fps and ms/frame on stderr once a second")
    args = p.parse_args()
    fourcc = args.fourcc.ljust(4)
    gains = tuple(float(x) for x in args.gains.split(","))
    order = BAYER_ORDER.get(args.fourcc, "bggr")
    if args.color:
        print("output nv12 %dx%d colour" % (args.width // 2, args.height // 2), file=sys.stderr)
    else:
        print("output nv12 %dx%d luma" % (args.width, args.height), file=sys.stderr)

    hold_sensor_powered()
    cam = open_camera(args.device, args.width, args.height, fourcc)
    out = sys.stdout.buffer
    try:  # a 64 KB pipe needs ~40 wake-ups per frame and drops the loop below the frame period; 1 MB needs 3
        fcntl.fcntl(out.fileno(), 1031, 1 << 20)  # F_SETPIPE_SZ
    except OSError:
        pass
    t0, n0, busy = time.monotonic(), 0, 0.0
    for n, (seq, ts_ns, data) in enumerate(cam.frames()):
        if args.frames and n >= args.frames:
            break
        t = time.monotonic()
        frame = color_nv12(data, cam.width, cam.height, order, gains) if args.color else luma_nv12(data, cam.width, cam.height)
        busy += time.monotonic() - t
        out.write(frame)
        out.flush()
        if args.stats and t - t0 >= 1.0:
            print("%.1f fps, %.1f ms/frame convert, seq %d" % ((n - n0) / (t - t0), busy / max(1, n - n0) * 1000, seq), file=sys.stderr)
            t0, n0, busy = t, n, 0.0


if __name__ == "__main__":
    main()
