"""Binder: frames in on stdin -> trigger index K and gap detection -> H.265 -> one SEI per
frame -> Annex-B file, plus a JSONL log (event vocabulary in camsync.matcher).

  fakecam | python3 -m camsync.binder --out rec.h265 --log rec.jsonl [--trigger-line 33] [--strobe-line 42]

SIGHUP tells the binder the trigger was re-enabled (new epoch, K restarts at 0).

Frames and encoded access units are paired by order. That holds only for an encoder that
emits exactly one access unit per input frame in input order (no B-frames, no dropping);
the binder logs encoder_lag while output falls more than ENCODER_LAG frames behind and
encoder_short at exit if frames never came out.
"""
import argparse
import collections
import json
import shlex
import signal
import subprocess
import sys
import threading
import time

from . import gpio
from .frame import read_frame
from .matcher import FLAG_DUP, Matcher
from .sei import access_units, build_sei, insert_sei, pack_record

ENCODERS = {
    "wave5": "v4l2h265enc",                                                             # J722S hardware encoder
    "x265": "videoconvert ! x265enc speed-preset=ultrafast tune=zerolatency option-string=bframes=0",  # host testing
}
ENCODER_LAG = 30
STATUS_S = 5


def pick_encoder():
    have_wave5 = subprocess.run(["gst-inspect-1.0", "v4l2h265enc"], capture_output=True).returncode == 0
    return "wave5" if have_wave5 else "x265"


def encoder_cmd(name, width, height, fps):
    return shlex.split(
        "gst-launch-1.0 -q fdsrc fd=0 ! rawvideoparse format=nv12 width=%d height=%d framerate=%d/1 colorimetry=bt709 "
        "! %s ! h265parse ! video/x-h265,stream-format=byte-stream,alignment=au ! fdsink fd=1"
        % (width, height, round(fps), ENCODERS[name]))


class Binder:
    def __init__(self, args):
        self.args = args
        self.log_file = open(args.log, "w")
        self.lock = threading.Lock()
        self.matcher = Matcher(round(1e9 / args.fps), args.check_s * 1_000_000_000, self.log)
        self.pending = collections.deque()      # SEI records in frame order, waiting for their access unit
        self.enc = self.writer = None
        self.out = open(args.out, "wb")
        self.frames = self.aus = self.bytes_out = 0
        self.lost = self.missed = self.dups = 0

    def log(self, ev):
        self.log_file.write(json.dumps(ev) + "\n")

    def start_encoder(self, width, height):
        name = self.args.encoder or pick_encoder()
        self.enc = subprocess.Popen(encoder_cmd(name, width, height, self.args.fps),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.writer = threading.Thread(target=self.write_stream, daemon=True)
        self.writer.start()

    def write_stream(self):
        for au in access_units(iter(lambda: self.enc.stdout.read(1 << 16), b"")):
            if not self.pending:
                self.log({"ev": "encoder_extra_au"})
                continue
            data = insert_sei(au, build_sei(self.pending.popleft()))
            self.out.write(data)
            self.aus += 1
            self.bytes_out += len(data)

    def thread(self, target):
        threading.Thread(target=target, daemon=True).start()

    def listen(self, line, consumer, handler):
        def loop():
            for ts_ns in gpio.EdgeListener(self.args.gpio_chip, line, consumer).events():
                with self.lock:
                    handler(ts_ns)
        self.thread(loop)

    def tick_loop(self):
        while True:
            time.sleep(1 / self.args.fps)
            with self.lock:
                self.matcher.tick(time.monotonic_ns())

    def rearm(self, *_):
        with self.lock:
            self.matcher.rearm()

    def run(self):
        a = self.args
        if a.trigger_line is not None:
            self.listen(a.trigger_line, "binder-trigger", self.matcher.edge)
            self.thread(self.tick_loop)
        if a.strobe_line is not None:
            self.listen(a.strobe_line, "binder-strobe", self.matcher.strobe)
        signal.signal(signal.SIGHUP, self.rearm)
        inp = sys.stdin.buffer
        t_status = t_time = 0
        while True:
            fr = read_frame(inp)
            if fr is None:
                break
            seq, ts_ns, width, height, sc, image = fr
            if self.enc is None:
                self.start_encoder(width, height)
            with self.lock:
                r = self.matcher.frame(ts_ns, sc)
                epoch = self.matcher.epoch
            self.pending.append(pack_record(epoch, r.k, ts_ns, sc, r.flags))
            self.enc.stdin.write(image)
            self.frames += 1
            self.lost += r.delivery_lost
            self.missed += r.sensor_missed
            self.dups += bool(r.flags & FLAG_DUP)
            self.log({"ev": "frame", "epoch": epoch, "k": r.k, "seq": seq, "s": sc, "ts_ns": ts_ns, "flags": r.flags})
            now = time.monotonic()
            if now - t_time >= 1:
                t_time = now
                self.log({"ev": "time", "epoch": epoch, "k": r.k, "ts_ns": ts_ns, "realtime_ns": time.time_ns(),
                          "edges": self.matcher.edges, "last_edge_ns": self.matcher.last_edge_ns})
            if now - t_status >= STATUS_S:
                t_status = now
                self.status()
        return self.finish()

    def status(self):
        lag = self.frames - self.aus
        if lag > ENCODER_LAG:
            self.log({"ev": "encoder_lag", "lag": lag})
        print("frames %d  aus %d  k %d  epoch %d  lost %d  missed %d  dups %d  out %.1f MB"
              % (self.frames, self.aus, self.matcher.k, self.matcher.epoch, self.lost, self.missed,
                 self.dups, self.bytes_out / 1e6), file=sys.stderr, flush=True)

    def finish(self):
        if self.enc:
            self.enc.stdin.close()
            self.enc.wait()
            self.writer.join()
        self.out.close()
        self.status()
        if self.pending:
            self.log({"ev": "encoder_short", "missing": len(self.pending)})
            print("ERROR: %d frames never came out of the encoder" % len(self.pending), file=sys.stderr)
        self.log_file.close()
        return 1 if self.pending else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="Annex-B .h265 output")
    p.add_argument("--log", required=True, help="JSONL log")
    p.add_argument("--fps", type=float, default=30.0, help="nominal trigger rate")
    p.add_argument("--encoder", choices=sorted(ENCODERS), default=None, help="default: wave5 if present, else x265")
    p.add_argument("--check-s", type=float, default=60.0, help="window for the K-versus-time check")
    p.add_argument("--gpio-chip", default="600000.gpio")
    p.add_argument("--trigger-line", type=int, default=None, help="GPIO0_33 = J28 pin 13, PWM loopback")
    p.add_argument("--strobe-line", type=int, default=None, help="GPIO0_42 = J28 pin 22, sensor strobe")
    sys.exit(Binder(p.parse_args()).run())


if __name__ == "__main__":
    main()
