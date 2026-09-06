"""Fake camera: behaves like a slaved sensor with an embedded frame counter and a strobe.

Writes frames (camsync.frame format) to stdout. Trigger edges come from an internal timer,
or from the FSIN_SIM GPIO when --fsin-line is given. Faults are keyed on the edge index e,
counted by the fake camera from its first edge:

  skip@e[:n]   sensor ignores n edges: no exposure, no counter step, no strobe, no frame
  drop@e       exposure happens (counter, strobe) but the frame is never delivered
  dup@e        the frame is delivered twice
  late@e:ms    the frame is delivered ms late and timestamped at delivery

The sensor counter is burnt into the picture as digits so a decoded file can be
checked by eye against the SEI and the log.
"""
import argparse
import itertools
import sys
import time

from . import gpio
from .frame import write_frame

BANK = 8
FONT = {  # 3x5 digits, rows top to bottom
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
}
SCALE = 12


def make_bank(width, height):
    """BANK NV12 frames: a horizontal luma gradient with a bright bar that moves each frame."""
    frames = []
    chroma = b"\x80" * (width * height // 2)
    for b in range(BANK):
        row = bytearray(int(x * 200 / width) + 16 for x in range(width))
        x0 = b * width // BANK
        row[x0:x0 + width // 16] = b"\xeb" * (width // 16)
        frames.append(bytes(row) * height + chroma)
    return frames


def burn_digits(image, width, text):
    x = 2 * SCALE
    for ch in text:
        for r, bits in enumerate(FONT[ch]):
            for c, bit in enumerate(bits):
                if bit == "1":
                    for dy in range(SCALE):
                        off = (2 * SCALE + r * SCALE + dy) * width + x + c * SCALE
                        image[off:off + SCALE] = b"\xff" * SCALE
        x += 4 * SCALE


def parse_faults(spec):
    """'skip@50:3,drop@100,dup@150,late@200:40' -> {50: ('skip', 3), 100: ('drop', 0), ...}"""
    faults = {}
    for item in filter(None, spec.split(",")):
        kind, _, rest = item.partition("@")
        e, _, arg = rest.partition(":")
        faults[int(e)] = (kind, float(arg or 0))
    return faults


def virtual_edges(fps):
    period_ns = round(1e9 / fps)
    t0 = time.monotonic_ns()
    for e in itertools.count():
        delay = t0 + e * period_ns - time.monotonic_ns()
        if delay > 0:
            time.sleep(delay / 1e9)
        yield t0 + e * period_ns


def run(args):
    out = sys.stdout.buffer
    faults = parse_faults(args.faults)
    if args.fsin_line is None:
        edges = virtual_edges(args.fps)
    else:
        edges = gpio.EdgeListener(args.gpio_chip, args.fsin_line, "fakecam-fsin").events()
    strobe = gpio.Output(args.gpio_chip, args.strobe_line, "fakecam-strobe") if args.strobe_line is not None else None
    bank = make_bank(args.width, args.height)
    counter = args.counter_start
    seq = 0
    skip_until = -1
    for e, _edge_ts in enumerate(edges):
        if args.frames and e >= args.frames:
            break
        kind, arg = faults.get(e, (None, 0))
        if kind == "skip":
            skip_until = e + max(1, int(arg)) - 1
        if e <= skip_until:
            continue
        if strobe:
            strobe.pulse()
        exposure = counter
        counter = (counter + 1) & 0xFFFF
        if kind == "drop":
            continue
        if kind == "late":
            time.sleep(arg / 1e3)
        image = bytearray(bank[e % BANK])
        burn_digits(image, args.width, "%05d" % exposure)
        for _ in range(2 if kind == "dup" else 1):
            write_frame(out, seq, time.monotonic_ns(), args.width, args.height, exposure, image)
            seq += 1
        out.flush()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1200)
    p.add_argument("--fps", type=float, default=30.0, help="internal trigger rate when no --fsin-line")
    p.add_argument("--gpio-chip", default="600000.gpio", help="main_gpio0 on the J722S")
    p.add_argument("--fsin-line", type=int, default=None, help="trigger from this GPIO (GPIO0_36 = J28 pin 37)")
    p.add_argument("--strobe-line", type=int, default=None, help="pulse this GPIO per exposure (GPIO0_41 = J28 pin 36)")
    p.add_argument("--counter-start", type=int, default=0, help="e.g. 65500 to exercise the 16-bit wrap")
    p.add_argument("--frames", type=int, default=0, help="stop after this many edges (0 = run forever)")
    p.add_argument("--faults", default="")
    run(p.parse_args())


if __name__ == "__main__":
    main()
