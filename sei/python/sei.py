"""Annex-B helpers: split a byte stream into access units, build and parse the camsync SEI.

The trigger index travels inside the video as a prefix SEI, payload type 5 (user_data_unregistered):
16-byte UUID then RECORD. Any decoder ignores it. Twin of sei/src/sei.cpp; both are tested on
sei/tests/vectors.txt. Copied from backup/python/camsync/sei.py, which stays as the frozen reference.

Two codecs, one record: the recording is H.265, the proxy substream of DESIGN_RING_CONTROL.md §8.3a is
H.264. Every function takes `codec="h265"|"h264"` and defaults to h265, so the recording path is unchanged.
"""
import re
import struct

UUID = bytes.fromhex("6a8f2b4e5d3c4a1b9e7f0c2d4b6a8e10")
RECORD = struct.Struct(">4sBHIQHH")   # magic, version, epoch, k, frame_ts_ns, sensor_count, flags
MAGIC = b"CSYN"
VERSION = 1
START = b"\x00\x00\x00\x01"
_SC3 = b"\x00\x00\x01"

# per codec: header bytes, VCL types, IDR types, prefix-SEI type, first parameter set, AU starters
_H265 = {"hdr": 2, "vcl": range(0, 32), "idr": (19, 20), "sei": 39, "first_ps": 32, "starters": {32, 33, 34, 35, 39}}
_H264 = {"hdr": 1, "vcl": range(1, 6), "idr": (5,), "sei": 6, "first_ps": 7, "starters": {6, 7, 8, 9}}
CODECS = {"h265": _H265, "h264": _H264}


def _c(codec):
    try:
        return CODECS[codec]
    except KeyError:
        raise ValueError("codec must be h265 or h264, not %r" % (codec,)) from None


def nal_type(nal, codec="h265"):
    return nal[0] & 0x1F if _c(codec) is _H264 else (nal[0] >> 1) & 0x3F


def is_vcl(nal, codec="h265"):
    return nal_type(nal, codec) in _c(codec)["vcl"]


def is_idr(nal, codec="h265"):
    return nal_type(nal, codec) in _c(codec)["idr"]


def is_first_param_set(nal, codec="h265"):
    """The parameter set that must open a stream: VPS (H.265) or SPS (H.264)."""
    return nal_type(nal, codec) == _c(codec)["first_ps"]


def _first_slice(nal, codec="h265"):
    hdr = _c(codec)["hdr"]
    return is_vcl(nal, codec) and len(nal) > hdr and bool(nal[hdr] & 0x80)


def split(data, codec="h265"):
    """Access units of a complete Annex-B buffer: list of (begin, end, [nal bytes]).

    A new AU starts at a first-slice VCL NAL, or at a VPS/SPS/PPS/AUD/prefix-SEI once the current AU
    holds a VCL NAL. The last AU runs to the end of the buffer whether or not it is complete.
    """
    starters = _c(codec)["starters"]
    data = bytes(data)
    aus, cur, begin, has_vcl, prev_end = [], [], 0, False, 0
    i = data.find(_SC3)
    while i >= 0:
        j = data.find(_SC3, i + 3)
        ne = j if j >= 0 else len(data)
        nb = i + 3
        while ne > nb and data[ne - 1] == 0:      # zero_byte of a following 4-byte start code
            ne -= 1
        sc_start = i - 1 if (i > prev_end and data[i - 1] == 0) else i
        if ne > nb:
            nal = data[nb:ne]
            if has_vcl and (_first_slice(nal, codec) or nal_type(nal, codec) in starters):
                aus.append((begin, sc_start, cur))
                cur, begin, has_vcl = [], sc_start, False
            cur.append(nal)
            has_vcl = has_vcl or is_vcl(nal, codec)
            prev_end = ne
        i = j
    if cur:
        aus.append((begin, len(data), cur))
    return aus


def access_units(chunks, codec="h265"):
    """Yield access units (lists of NAL units without start codes) from Annex-B chunks."""
    for _, _, nals in split(b"".join(chunks), codec):
        yield nals


def last_complete_au_end(data, codec="h265"):
    """End offset of the last AU that is followed by another one (provably complete); 0 if none."""
    aus = split(data, codec)
    return aus[-2][1] if len(aus) >= 2 else 0


def _add_epb(data):
    # non-overlapping matches = the zero counter restarts after an inserted 0x03, as the spec says
    return re.sub(rb"\x00\x00(?=[\x00-\x03])", b"\x00\x00\x03", data)


def _strip_epb(data):
    return re.sub(rb"\x00\x00\x03", b"\x00\x00", data)


def pack_record(epoch, k, frame_ts_ns, sensor_count, flags):
    return RECORD.pack(MAGIC, VERSION, epoch, k, frame_ts_ns, sensor_count, flags)


def build_sei(record, codec="h265"):
    payload = UUID + record
    body = bytes([5, len(payload)]) + payload + b"\x80"
    head = bytes([6]) if _c(codec) is _H264 else bytes([39 << 1, 1])   # H.264: nal_ref_idc 0, SEI
    return START + head + _add_epb(body)


def parse_sei(nal, codec="h265"):
    """Return (epoch, k, frame_ts_ns, sensor_count, flags) if nal is our SEI, else None."""
    spec = _c(codec)
    if nal_type(nal, codec) != spec["sei"]:
        return None
    body = _strip_epb(nal[spec["hdr"]:])
    if len(body) < 2 or body[0] != 5:
        return None
    payload = body[2:2 + body[1]]
    if payload[:16] != UUID or len(payload) < 16 + RECORD.size:
        return None
    magic, version, epoch, k, ts, sc, flags = RECORD.unpack_from(payload, 16)
    if magic != MAGIC or version != VERSION:
        return None
    return epoch, k, ts, sc, flags


def insert_sei(au, sei, codec="h265"):
    """Serialize an access unit (list of NALs) with sei placed before its first VCL NAL."""
    out = bytearray()
    for nal in au:
        if sei and is_vcl(nal, codec):
            out += sei
            sei = None
        out += START + nal
    return bytes(out)


def record_of(au, codec="h265"):
    for nal in au:
        rec = parse_sei(nal, codec)
        if rec:
            return rec
    return None
