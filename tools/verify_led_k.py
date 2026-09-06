#!/usr/bin/env python3
"""Does the K in the picture equal the K in the metadata?

The one test that proves the whole chain. An external controller drives a row of LEDs showing the
trigger index in binary and also generates the trigger, so the LEDs are lit by the same pulse that
exposes the frame. This reads the LEDs out of the decoded picture and compares them with the trigger
index the tile wrote into that frame's SEI.

    tools/verify_led_k.py --preview rec.h265                    find the LEDs: a brightness map
    tools/verify_led_k.py --roi 40,30,320,40 --bits 8 rec.h265  the check
    tools/verify_led_k.py --roi ... --bits 8 --codec h264 live.h264

The verdict is about the *offset*, not equality. The controller and the tile need not start counting
at the same pulse, so a constant `led - sei` across every frame is a pass: the two agree on which pulse
took which picture. An offset that changes is a real misalignment, and the frame where it changes is
named. Exits non-zero on a mismatch, so it drops into CI.

Needs gst-launch-1.0 with a decoder (avdec_h265 / avdec_h264). No numpy, no OpenCV.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sei", "python"))
import sei  # noqa: E402

RAMP = " .:-=+*#%@"


def decode(path, codec, w, h):
    """Yield each decoded picture as GRAY8 bytes, scaled to w x h."""
    parser, dec = ("h264parse", "avdec_h264") if codec == "h264" else ("h265parse", "avdec_h265")
    pipe = ("filesrc location=%s ! %s ! %s ! videoconvert ! videoscale ! "
            "video/x-raw,format=GRAY8,width=%d,height=%d ! fdsink fd=1" % (path, parser, dec, w, h))
    p = subprocess.Popen(["gst-launch-1.0", "-q"] + pipe.split(),
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            frame = p.stdout.read(w * h)
            if len(frame) < w * h:
                return
            yield frame
    finally:
        p.kill()
        p.wait()


def sei_indices(path, codec):
    """(epoch, K) per access unit, in order, None where a frame carries no record."""
    with open(path, "rb") as f:
        data = f.read()
    out = []
    for au in sei.split(data, codec):
        nals = au[2] if isinstance(au, tuple) and len(au) == 3 else au
        r = sei.record_of(nals, codec)
        out.append(None if r is None else (r[0], r[1]))
    return out


def preview(path, codec, n, cols=96, rows=32):
    """A coarse brightness map of one frame, with a coordinate grid, to locate the LEDs by eye."""
    for i, frame in enumerate(decode(path, codec, cols, rows)):
        if i < n:
            continue
        print("frame %d, %d x %d cells; each cell is 1/%d of the width, 1/%d of the height"
              % (n, cols, rows, cols, rows))
        print("     " + "".join(str((c // 10) % 10) if c % 10 == 0 else " " for c in range(cols)))
        print("     " + "".join(str(c % 10) for c in range(cols)))
        for r in range(rows):
            line = "".join(RAMP[min(len(RAMP) - 1, frame[r * cols + c] * len(RAMP) // 256)]
                           for c in range(cols))
            print("%3d  %s" % (r, line))
        print("\nThe LEDs are the bright run. Read off its first column, row, width and height as\n"
              "fractions of the grid, then give --roi as pixel values in the *scaled* image you pass\n"
              "with --width/--height (default 256x64), or just use the same grid: --grid %d,%d." % (cols, rows))
        return 0
    print("no frame %d in %s" % (n, path), file=sys.stderr)
    return 2


def read_bits(frame, w, roi, bits, order, invert):
    """The LED word in one frame: split the ROI into `bits` columns, threshold at the ROI midpoint."""
    x, y, rw, rh = roi
    cells = []
    for b in range(bits):
        x0 = x + rw * b // bits
        x1 = x + rw * (b + 1) // bits
        acc = n = 0
        for yy in range(y, y + rh):
            row = yy * w
            for xx in range(x0, x1):
                acc += frame[row + xx]
                n += 1
        cells.append(acc / max(n, 1))
    lo, hi = min(cells), max(cells)
    if hi - lo < 24:                      # no contrast: all on, all off, or the ROI is wrong
        return None, cells
    mid = (lo + hi) / 2
    word = 0
    seq = cells if order == "msb" else list(reversed(cells))
    for c in seq:
        bit = (c < mid) if invert else (c > mid)
        word = (word << 1) | int(bit)
    return word, cells


def main():
    ap = argparse.ArgumentParser(description="LED index in the picture versus the index in the metadata")
    ap.add_argument("file")
    ap.add_argument("--codec", default="h265", choices=("h265", "h264"))
    ap.add_argument("--preview", nargs="?", type=int, const=0, default=None,
                    help="print a brightness map of frame N and exit")
    ap.add_argument("--roi", help="X,Y,W,H of the LED row, in the scaled image")
    ap.add_argument("--bits", type=int, default=8)
    ap.add_argument("--order", default="msb", choices=("msb", "lsb"), help="which end is the high bit")
    ap.add_argument("--invert", action="store_true", help="dark cell means 1")
    ap.add_argument("--width", type=int, default=256, help="decode width the ROI is measured in")
    ap.add_argument("--height", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0, help="stop after N frames")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.preview is not None:
        return preview(a.file, a.codec, a.preview)
    if not a.roi:
        print("need --roi X,Y,W,H (use --preview to find it)", file=sys.stderr)
        return 2
    roi = tuple(int(v) for v in a.roi.split(","))
    if len(roi) != 4:
        print("--roi wants X,Y,W,H", file=sys.stderr)
        return 2

    idx = sei_indices(a.file, a.codec)
    offsets = {}
    unread = nosei = 0
    first_bad = None
    n = 0
    for i, frame in enumerate(decode(a.file, a.codec, a.width, a.height)):
        if i >= len(idx):
            break
        if a.limit and n >= a.limit:
            break
        n += 1
        rec = idx[i]
        led, cells = read_bits(frame, a.width, roi, a.bits, a.order, a.invert)
        if rec is None:
            nosei += 1
            continue
        if led is None:
            unread += 1
            if a.verbose:
                print("frame %-5d LED unreadable (cell means %s)" % (i, [round(c) for c in cells]))
            continue
        off = led - rec[1]
        offsets[off] = offsets.get(off, 0) + 1
        if a.verbose:
            print("frame %-5d LED %-6d SEI epoch %d K %-8d offset %d" % (i, led, rec[0], rec[1], off))
        elif first_bad is None and len(offsets) > 1:
            first_bad = (i, led, rec[1], off)

    if not offsets:
        print("no frame had both a readable LED word and an SEI (%d unreadable, %d without SEI)"
              % (unread, nosei), file=sys.stderr)
        return 2
    best, count = max(offsets.items(), key=lambda kv: kv[1])
    total = sum(offsets.values())
    print("frames compared      %d" % total)
    print("LED unreadable       %d" % unread)
    print("frames without SEI   %d" % nosei)
    print("offset led - K       %d on %d frames (%.1f%%)" % (best, count, 100.0 * count / total))
    if len(offsets) == 1:
        print("\nPASS: every frame agrees, offset %d. The index in the picture and the index in the\n"
              "metadata name the same pulse." % best)
        return 0
    print("\nFAIL: %d different offsets %s" % (len(offsets), sorted(offsets)))
    if first_bad:
        print("first disagreement at frame %d: LED %d, SEI K %d, offset %d"
              % first_bad)
    print("A changing offset means a picture was paired with the wrong pulse. Re-run with --verbose.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
