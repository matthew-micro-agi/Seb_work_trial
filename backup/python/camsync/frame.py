"""Frame record shared by a frame source (fakecam today, the V4L2 camera later) and the binder.

Wire format on the pipe, per frame:
  HEADER  magic, seq, ts_ns, width, height, sensor_count   (little-endian, 22 bytes)
  image   width * height * 3 // 2 bytes of NV12

seq and ts_ns are what V4L2 gives for a real camera: the driver's sequence number and
the CLOCK_MONOTONIC timestamp taken when the frame was delivered. sensor_count is the
sensor's own 16-bit frame counter; the source parses it out of the sensor's embedded
data rows (AR0234: register 0x303A), so the binder never sees a sensor-specific layout.
"""
import struct

MAGIC = b"CSF1"
HEADER = struct.Struct("<4sIQHHH")


def nv12_size(width, height):
    return width * height * 3 // 2


def write_frame(out, seq, ts_ns, width, height, sensor_count, image):
    out.write(HEADER.pack(MAGIC, seq, ts_ns, width, height, sensor_count))
    out.write(image)


def read_frame(inp):
    """Return (seq, ts_ns, width, height, sensor_count, image), or None at a clean EOF."""
    hdr = inp.read(HEADER.size)
    if not hdr:
        return None
    if len(hdr) < HEADER.size:
        raise EOFError("truncated frame header")
    magic, seq, ts_ns, width, height, sensor_count = HEADER.unpack(hdr)
    if magic != MAGIC:
        raise ValueError("bad frame magic %r" % magic)
    image = inp.read(nv12_size(width, height))
    if len(image) < nv12_size(width, height):
        raise EOFError("truncated frame image")
    return seq, ts_ns, width, height, sensor_count, image
