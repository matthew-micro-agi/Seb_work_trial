"""Live preview through GStreamer from Python, no pipe: V4L2 frames are converted to NV12 and pushed
into an appsrc, then Wave5 H.265, then a TCP server (Annex-B byte stream) that the laptop viewer
reads through an SSH port forward. Buffer PTS = the V4L2 capture timestamp (CLOCK_MONOTONIC),
which is what the trigger-index matching will key on later.

  python3 -m camsync.gststream --device /dev/video3 [--color] [--port 5600] [--bind 127.0.0.1]
"""
import argparse
import queue
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from .stream import BAYER_ORDER, color_nv12, hold_sensor_powered, luma_nv12, open_camera  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="/dev/video3")
    p.add_argument("--width", type=int, default=1456)
    p.add_argument("--height", type=int, default=1088)
    p.add_argument("--fourcc", default="BG10")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--color", action="store_true")
    p.add_argument("--gains", default="1.6,1.0,1.4")
    p.add_argument("--bitrate", type=int, default=6000000)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5600)
    p.add_argument("--frames", type=int, default=0)
    p.add_argument("--sink", default=None, help="override the sink description, e.g. 'fakesink' or 'filesink location=/tmp/x.h265'")
    args = p.parse_args()
    fourcc = args.fourcc.ljust(4)
    gains = tuple(float(x) for x in args.gains.split(","))
    order = BAYER_ORDER.get(args.fourcc, "bggr")
    ow, oh = (args.width // 2, args.height // 2) if args.color else (args.width, args.height)

    Gst.init(None)
    sink = args.sink or "tcpserversink host=%s port=%d sync=false" % (args.bind, args.port)
    desc = ("appsrc name=src is-live=true format=time do-timestamp=false block=true max-bytes=%d "
            "caps=video/x-raw,format=NV12,width=%d,height=%d,framerate=%d/1,colorimetry=bt709,interlace-mode=progressive,pixel-aspect-ratio=1/1 "
            "! queue max-size-buffers=3 leaky=downstream "
            "! v4l2h265enc extra-controls=\"controls,video_gop_size=%d,video_bitrate=%d\" "
            "! h265parse config-interval=1 ! video/x-h265,stream-format=byte-stream,alignment=au ! %s"
            % (ow * oh * 3 // 2 * 4, ow, oh, args.fps, args.fps, args.bitrate, sink))
    print("pipeline: " + desc, file=sys.stderr)
    pipeline = Gst.parse_launch(desc)
    src = pipeline.get_by_name("src")
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)

    hold_sensor_powered()
    cam = open_camera(args.device, args.width, args.height, fourcc)
    print("streaming %dx%d %s -> %s" % (ow, oh, "colour" if args.color else "luma", sink), file=sys.stderr)

    # The V4L2 buffers are uncached DMA memory: copying a 3 MB frame out costs ~20 ms on one A53, the
    # conversion another 7 ms, together a whole frame period. A capture thread does the copy while this
    # thread converts the previous frame (numpy releases the GIL), so the two overlap.
    frames = queue.Queue(maxsize=2)

    def capture():
        for item in cam.frames():
            try:
                frames.put(item, timeout=2)
            except queue.Full:
                return
    threading.Thread(target=capture, daemon=True).start()

    t0, n0, busy, first_ts, n = time.monotonic(), 0, 0.0, None, -1
    while True:
        n += 1
        if args.frames and n >= args.frames:
            break
        seq, ts_ns, data = frames.get()
        t = time.monotonic()
        nv12 = color_nv12(data, cam.width, cam.height, order, gains) if args.color else luma_nv12(data, cam.width, cam.height)
        busy += time.monotonic() - t
        buf = Gst.Buffer.new_wrapped(nv12)
        first_ts = ts_ns if first_ts is None else first_ts
        buf.pts = ts_ns - first_ts                       # V4L2 CLOCK_MONOTONIC capture time, relative to the first frame
        buf.duration = Gst.SECOND // args.fps
        buf.offset = seq                                 # V4L2 sequence number rides along
        if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            print("push-buffer failed", file=sys.stderr)
            break
        msg = bus.pop_filtered(Gst.MessageType.ERROR)
        if msg:
            print("gst error: %s" % msg.parse_error()[0].message, file=sys.stderr)
            break
        if t - t0 >= 1.0:
            print("%.1f fps, %.1f ms/frame convert, seq %d" % ((n - n0) / (t - t0), busy / max(1, n - n0) * 1000, seq), file=sys.stderr)
            t0, n0, busy = t, n, 0.0
    src.emit("end-of-stream")
    bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
