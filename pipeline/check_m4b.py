"""M4b checker: the recording's SEIs equal the capture log, every proxy SEI equals the recording's record
for the same frame, no stamp was lost, and (--pixels) the barcode in the decoded picture equals the frame
the SEI names. The recording is read with backup/python/camsync/sei.py, the frozen HEVC reference; the
proxy is H.264 (DESIGN_RING_CONTROL.md §8.3a) and is read with the sei/python twin.

  python3 pipeline/check_m4b.py rec.h265 rec.jsonl [--proxy proxy.h264] [--pixels] [--decoder avdec_h265]
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backup" / "python"))
sys.path.insert(0, str(ROOT / "sei" / "python"))
from camsync.sei import access_units, record_of  # noqa: E402
import sei as sei_twin  # noqa: E402

STAMP_LOST = 1 << 15


def records(path):
    with open(path, "rb") as f:
        aus = list(access_units(iter(lambda: f.read(1 << 20), b"")))
    recs = [record_of(au) for au in aus]
    return recs, recs.count(None)


def records_h264(path):
    with open(path, "rb") as f:
        aus = list(sei_twin.access_units([f.read()], "h264"))
    recs = [sei_twin.record_of(au, "h264") for au in aus]
    keys = sum(1 for au in aus if any(sei_twin.is_idr(n, "h264") for n in au))
    return recs, recs.count(None), keys


def barcodes(path, decoder):
    """Frame index read from each decoded picture: 16 blocks over the top half, MSB left."""
    parser = "h264parse" if "264" in decoder else "h265parse"
    pipe = ("filesrc location=%s ! %s ! %s ! videoconvert ! videoscale ! "
            "video/x-raw,format=GRAY8,width=256,height=16 ! fdsink fd=1" % (path, parser, decoder))
    p = subprocess.Popen(["gst-launch-1.0", "-q"] + pipe.split(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    while True:
        frame = p.stdout.read(256 * 16)
        if len(frame) < 256 * 16:
            break
        n = 0
        for j in range(16):
            mean = sum(frame[r * 256 + j * 16 + c] for r in range(8) for c in range(16)) / 128
            n = n << 1 | (mean > 128)
        out.append(n)
    p.wait()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rec"); ap.add_argument("log"); ap.add_argument("--proxy"); ap.add_argument("--pixels", action="store_true")
    ap.add_argument("--decoder", default="avdec_h265")
    ap.add_argument("--proxy-decoder", default="avdec_h264")
    a = ap.parse_args()
    ok = True

    with open(a.log) as f:
        events = [json.loads(line) for line in f]
    logged = [(e["epoch"], e["k"], e["ts_ns"], e["s"], e["flags"] & 0xFFFF) for e in events if e["ev"] == "frame"]
    for e in events:
        if e["ev"] in ("stamp_lost", "enc_pts_offset"):
            print("log event:", e)
    rec, rec_missing = records(a.rec)
    lost = sum(1 for r in rec if r and r[4] & STAMP_LOST)
    same = rec == logged
    print("recording: %d AUs, %d without SEI, %d stamp lost; log frames %d; SEI == log: %s"
          % (len(rec), rec_missing, lost, len(logged), "IDENTICAL" if same else "DIFFER"))
    ok = ok and rec_missing == 0 and lost == 0 and same

    if a.proxy:
        prox, prox_missing, keys = records_h264(a.proxy)
        known = set(rec)
        foreign = [r for r in prox if r not in known]
        plost = sum(1 for r in prox if r and r[4] & STAMP_LOST)
        print("proxy (h264): %d AUs, %d IDR, %d without SEI, %d stamp lost, %d records not in the recording"
              % (len(prox), keys, prox_missing, plost, len(foreign)))
        for r in foreign[:5]:
            print("   foreign:", r)
        ok = ok and prox_missing == 0 and plost == 0 and not foreign and len(prox) > 0 and keys > 0

    if a.pixels:
        for name, path, recs, dec in ([("recording", a.rec, rec, a.decoder)]
                                      + ([("proxy", a.proxy, prox, a.proxy_decoder)] if a.proxy else [])):
            codes = barcodes(path, dec)
            bad = [(i, c, r[3]) for i, (c, r) in enumerate(zip(codes, recs)) if r and c != r[3]]
            print("%s pixels: %d decoded frames, %d SEIs, %d barcode != SEI frame" % (name, len(codes), len(recs), len(bad)))
            for i, c, s in bad[:5]:
                print("   AU %d: picture says frame %d, SEI says %d" % (i, c, s))
            ok = ok and len(codes) == len(recs) and not bad

    print("OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
