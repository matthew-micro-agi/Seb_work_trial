"""HEVC Annex-B helpers: split a byte stream into access units, build and parse our SEI.

The trigger index travels inside the video as a prefix SEI, payload type 5
(user_data_unregistered): 16-byte UUID then RECORD. Any decoder ignores it.
"""
import itertools
import re
import struct

UUID = bytes.fromhex("6a8f2b4e5d3c4a1b9e7f0c2d4b6a8e10")
RECORD = struct.Struct(">4sBHIQHH")   # magic, version, epoch, k, frame_ts_ns, sensor_count, flags
MAGIC = b"CSYN"
VERSION = 1
START = b"\x00\x00\x00\x01"
_SC3 = b"\x00\x00\x01"
_AU_STARTERS = {32, 33, 34, 35, 39}   # VPS, SPS, PPS, AUD, prefix SEI


def nal_type(nal):
    return (nal[0] >> 1) & 0x3F


def _is_vcl(nal):
    return nal_type(nal) < 32


def _first_slice(nal):
    return _is_vcl(nal) and bool(nal[2] & 0x80)


def access_units(chunks):
    """Yield access units (lists of NAL units without start codes) from Annex-B chunks."""
    buf = bytearray()
    au, has_vcl = [], False
    for chunk in itertools.chain(chunks, (_SC3,)):        # the sentinel closes the last NAL
        buf += chunk
        while (i := buf.find(_SC3)) >= 0 and (j := buf.find(_SC3, i + 3)) >= 0:
            nal = bytes(buf[i + 3:j]).rstrip(b"\0")        # zero_byte of a following 4-byte start code
            del buf[:j]
            if not nal:
                continue
            if has_vcl and (_first_slice(nal) or nal_type(nal) in _AU_STARTERS):
                yield au
                au, has_vcl = [], False
            au.append(nal)
            has_vcl = has_vcl or _is_vcl(nal)
    if au:
        yield au


def _add_epb(data):
    # non-overlapping matches = the zero counter restarts after an inserted 0x03, as the spec says
    return re.sub(rb"\x00\x00(?=[\x00-\x03])", b"\x00\x00\x03", data)


def _strip_epb(data):
    return re.sub(rb"\x00\x00\x03", b"\x00\x00", data)


def pack_record(epoch, k, frame_ts_ns, sensor_count, flags):
    return RECORD.pack(MAGIC, VERSION, epoch, k, frame_ts_ns, sensor_count, flags)


def build_sei(record):
    payload = UUID + record
    body = bytes([5, len(payload)]) + payload + b"\x80"
    return START + bytes([39 << 1, 1]) + _add_epb(body)


def parse_sei(nal):
    """Return (epoch, k, frame_ts_ns, sensor_count, flags) if nal is our SEI, else None."""
    if nal_type(nal) != 39:
        return None
    body = _strip_epb(nal[2:])
    if len(body) < 2 or body[0] != 5:
        return None
    payload = body[2:2 + body[1]]
    if payload[:16] != UUID or len(payload) < 16 + RECORD.size:
        return None
    magic, version, epoch, k, ts, sc, flags = RECORD.unpack_from(payload, 16)
    if magic != MAGIC or version != VERSION:
        return None
    return epoch, k, ts, sc, flags


def insert_sei(au, sei):
    """Serialize an access unit with sei placed before its first VCL NAL."""
    out = bytearray()
    for nal in au:
        if sei and _is_vcl(nal):
            out += sei
            sei = None
        out += START + nal
    return bytes(out)


def record_of(au):
    for nal in au:
        rec = parse_sei(nal)
        if rec:
            return rec
    return None
