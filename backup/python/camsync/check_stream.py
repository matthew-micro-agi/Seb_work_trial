"""Verify a recorded .h265 file: every access unit carries our SEI, K never goes backwards
within an epoch, and (optionally) the K sequence matches the binder log.

  python3 -m camsync.check_stream rec.h265 [rec.jsonl]
"""
import json
import sys

from .sei import access_units, record_of


def scan(path):
    with open(path, "rb") as f:
        aus = list(access_units(iter(lambda: f.read(1 << 20), b"")))
    records = [record_of(au) for au in aus]
    return [r for r in records if r], records.count(None)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    recs, missing = scan(sys.argv[1])
    ks = [(r[0], r[1]) for r in recs]
    pairs = [(a, b) for a, b in zip(ks, ks[1:]) if a[0] == b[0]]
    jumps = [(a, b) for a, b in pairs if b[1] > a[1] + 1]
    backwards = sum(1 for a, b in pairs if b[1] < a[1])
    dups = sum(1 for a, b in pairs if a == b)
    print("access units %d  with SEI %d  without SEI %d" % (len(recs) + missing, len(recs), missing))
    print("epochs %s  K first %s last %s  jumps %d  backwards %d  duplicates %d"
          % (sorted({e for e, _ in ks}), ks[0][1] if ks else None, ks[-1][1] if ks else None,
             len(jumps), backwards, dups))
    for a, b in jumps:
        print("  gap in epoch %d: K %d -> %d" % (a[0], a[1], b[1]))
    ok = missing == 0 and backwards == 0
    if len(sys.argv) > 2:
        with open(sys.argv[2]) as f:
            logged = [(ev["epoch"], ev["k"]) for ev in map(json.loads, f) if ev["ev"] == "frame"]
        same = logged == ks
        print("log frames %d  file frames %d  K sequences %s" % (len(logged), len(ks), "IDENTICAL" if same else "DIFFER"))
        ok = ok and same
    print("OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
