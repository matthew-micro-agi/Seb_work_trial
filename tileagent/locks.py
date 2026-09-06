"""Locks are hard links: lock/<id>/<segno>.h265 next to seg/<segno>.h265 (DESIGN_RING_CONTROL.md §6).
The map {id -> set(segno)} is rebuilt from one readdir at start and maintained by our own link/unlink."""
import json
import os
import threading


def seg_name(segno):
    return "%012d.h265" % segno


class Locks:
    def __init__(self, ring):
        self.dir = os.path.join(ring, "lock")
        self.seg = os.path.join(ring, "seg")
        self.lock = threading.RLock()
        self.map = {}    # id -> set(segno)
        self.meta = {}   # id -> {"id", "reason", "requester", "created_ptp_ns"}
        os.makedirs(self.dir, exist_ok=True)
        for lid in sorted(os.listdir(self.dir)):
            d = os.path.join(self.dir, lid)
            if not os.path.isdir(d):
                continue
            segnos = {int(n[:12]) for n in os.listdir(d) if n.endswith(".h265") and n[:12].isdigit()}
            try:
                with open(os.path.join(d, ".meta")) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {"id": lid, "reason": "", "requester": "", "created_ptp_ns": 0}
            self.map[lid] = segnos
            self.meta[lid] = meta

    def ids(self):
        with self.lock:
            return sorted(self.map)

    def has(self, lid):
        return lid in self.map

    def segnos(self, lid):
        with self.lock:
            return set(self.map.get(lid, ()))

    def holders(self, segno):
        with self.lock:
            return sorted(lid for lid, s in self.map.items() if segno in s)

    def locked_segnos(self):
        with self.lock:
            out = set()
            for s in self.map.values():
                out |= s
            return out

    def path_of(self, segno):
        """A path where the segment's data exists now: seg/ first, else any lock; None if gone."""
        p = os.path.join(self.seg, seg_name(segno))
        if os.path.exists(p):
            return p
        for lid in self.holders(segno):
            p = os.path.join(self.dir, lid, seg_name(segno))
            if os.path.exists(p):
                return p
        return None

    def put(self, lid, segnos, meta):
        """Link every segno into lock/<lid>/. Returns (created, added, missing). Idempotent."""
        with self.lock:
            created = lid not in self.map
            d = os.path.join(self.dir, lid)
            if created:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, ".meta"), "w") as f:
                    json.dump(meta, f)
                self.map[lid] = set()
                self.meta[lid] = meta
            added, missing = [], []
            for s in sorted(segnos):
                if s in self.map[lid]:
                    continue
                target = os.path.join(d, seg_name(s))
                src = self.path_of(s)
                if src is None:
                    missing.append(s)
                    continue
                try:
                    os.link(src, target)
                except FileExistsError:
                    pass
                except FileNotFoundError:          # reclaimed between path_of and link
                    missing.append(s)
                    continue
                self.map[lid].add(s)
                added.append(s)
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return created, added, missing

    def delete(self, lid):
        """Unlink every member and the directory; returns the segnos it held."""
        with self.lock:
            d = os.path.join(self.dir, lid)
            for n in os.listdir(d):
                os.unlink(os.path.join(d, n))
            os.rmdir(d)
            self.meta.pop(lid, None)
            return self.map.pop(lid, set())
