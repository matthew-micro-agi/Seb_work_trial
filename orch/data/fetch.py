"""orch fetch (PLAN_ORCH.md §5): a selector -> <out>/<node>/seg/%012d.h265 files, byte for byte, resumable.

Order: GetNode, GetTime x5 (or the manifest's offset on resume), GetHealth bounds, ListSegments, K chain,
manifest, then per window: PutLock by segno range (507 -> a window that fits), GET x --jobs with .part
resume, DeleteLock if this run created the lock. Done is decided by content: a file whose size equals the
listed bytes. Ctrl-C keeps the lock and prints how to release or resume it.
"""
import concurrent.futures
import json
import os
import secrets
import threading
import time

import sei

from .. import clock, pb
from ..client import Client, TileError, load_nodes, node_dir

CHUNK = 1 << 20


class Refused(Exception):
    pass


class Node:
    """One tile's fetch. Prints through say(); returns the counts."""

    def __init__(self, client, out, args, say, between_list_and_copy=None):
        self.c, self.args, self.say = client, args, say
        self.node = client.rpc("GetNode")
        self.dir = os.path.join(out, node_dir(self.node.node_id))
        self.seg_dir = os.path.join(self.dir, "seg")
        os.makedirs(self.seg_dir, exist_ok=True)
        self.manifest_path = os.path.join(self.dir, "manifest.json")
        self.manifest = self._read_manifest()
        self.hook = between_list_and_copy            # tests: called after the list, before the copy
        self.stop = threading.Event()
        self.counts = {"done": 0, "skipped": 0, "resumed": 0, "vanished": 0, "short": 0, "bytes": 0}
        self.lock_id, self.lock_created = self.manifest.get("lock"), bool(self.manifest.get("lock"))
        self.rows = []

    def _read_manifest(self):
        try:
            with open(self.manifest_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, **extra):
        self.manifest.update(extra)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.manifest, f, indent=1)
        os.replace(tmp, self.manifest_path)

    # ── steps 2-5: calibrate, bounds, list, chain ────────────────────────────────────────────────────
    def select(self):
        a = self.args
        if "segments" not in self.node.capabilities:
            raise Refused("this node does not declare segments")
        kind = clock.selector_kind(a)
        cal = clock.calibrate(self.c.time_samples())
        old = self.manifest.get("time")
        if old and old.get("boot_id") != cal.boot_id:
            raise Refused("tile rebooted since this directory was written (boot %s -> %s): the recorded offset is void; use a new --out" % (old.get("boot_id", "?")[:8], cal.boot_id[:8]))
        if old:
            cal.offset_ns = old["offset_ns"]           # the same selection as the run being resumed
        for w in cal.warnings():
            self.say("warning: " + w)
        h = self.c.rpc("GetHealth")
        sel = clock.build_selector(a, cal)
        request = {"selector": json.loads(pb.to_json(sel))}
        if kind == "ptp":
            since, until = clock.wall_interval(a)
            request.update(since_wall=clock.fmt_wall(since), until_wall=clock.fmt_wall(until))
            e = clock.edges(sel.ptp.since_ns, sel.ptp.until_ns, h.ring.oldest_ptp_ns, h.ring.newest_ptp_ns)
            present = "%s .. %s" % (clock.fmt_wall(cal.to_wall(h.ring.oldest_ptp_ns)), clock.fmt_wall(cal.to_wall(h.ring.newest_ptp_ns))) if h.ring.oldest_ptp_ns else "nothing"
            if e is None:
                raise Refused("requested %s .. %s; footage on this tile spans %s" % (request["since_wall"], request["until_wall"], present))
            if e["head_missing_s"] or e["tail_missing_s"]:
                self.say("footage present %s: head missing %.0f s, tail missing %.0f s" % (present, e["head_missing_s"], e["tail_missing_s"]))
            request["edges"] = e
        rows = list(self.c.rpc("ListSegments", pb.ListSegmentsRequest(select=sel)).segments)
        if kind == "ptp" and not cal.ptp_locked:
            foreign = [s for s in rows if clock.same_boot(s.session, cal.boot_id) is False]
            recovered = [s for s in rows if clock.same_boot(s.session, cal.boot_id) is None]
            if foreign:
                self.say("dropped %d segments recorded in another boot (PTP time is per boot while unlocked)" % len(foreign))
                rows = [s for s in rows if clock.same_boot(s.session, cal.boot_id) is not False]
            if recovered:
                self.say("warning: %d segments come from a rebuilt index with no session; their boot is unknown, fetched anyway" % len(recovered))
        self.rows = rows
        self.chain(rows)
        self.say("%d segments, %.1f MB, K %s" % (len(rows), sum(s.bytes for s in rows) / 1e6, self._krange(rows)))
        self._write_manifest(node=json.loads(pb.to_json(self.node)), time=dict(cal.as_dict(), wall_ns=time.time_ns()),
                             request=request, segments=json.loads(pb.to_json(pb.SegmentList(segments=rows))))
        return rows

    @staticmethod
    def _krange(rows):
        m = [s for s in rows if s.HasField("first")]
        return "%d:%d..%d:%d" % (m[0].first.epoch, m[0].first.k, m[-1].last.epoch, m[-1].last.k) if m else "-"

    def chain(self, rows):
        """Gaps between consecutive listed segments, printed with fault names; K must step by one across
        fault-free neighbours of the same epoch."""
        gaps = 0
        for a, b in zip(rows, rows[1:]):
            if not (a.HasField("last") and b.HasField("first")):
                continue
            faults = [pb.Fault.Name(f) for f in list(a.faults) + list(b.faults)]
            if a.last.epoch != b.first.epoch:
                self.say("epoch change between segments %d and %d: K %d:%d -> %d:%d %s" % (a.segno, b.segno, a.last.epoch, a.last.k, b.first.epoch, b.first.k, faults))
            elif b.first.k != a.last.k + 1:
                gaps += 1
                self.say("K gap between segments %d and %d: %d -> %d (%d frames) faults=%s" % (a.segno, b.segno, a.last.k, b.first.k, b.first.k - a.last.k - 1, faults or "-"))
        return gaps

    # ── steps 6-8: lock windows and the copy ─────────────────────────────────────────────────────────
    def run(self):
        rows = self.select()
        if self.hook:
            self.hook(self)
        if "sessions" in self.node.capabilities:
            self.session_logs(rows)
        locking = "locks" in self.node.capabilities and not self.args.no_lock
        remaining = rows
        try:
            while remaining and not self.stop.is_set():
                window, consumed = self.lock_window(remaining) if locking else (remaining, len(remaining))
                self.copy(window)
                if locking and self.lock_created and not self.args.keep_lock and not self.stop.is_set():
                    self.c.rpc("DeleteLock", pb.LockId(id=self.lock_id))
                    self._write_manifest(lock=None)
                    self.lock_created = False
                remaining = remaining[consumed:]
        except KeyboardInterrupt:
            self.stop.set()
        if self.stop.is_set():
            self.say("interrupted; %s\n  orch fetch with the same --out resumes" % (
                "lock %s kept: `orch unlock %s` releases it" % (self.lock_id, self.lock_id) if self.lock_created else "no lock held"))
        c = self.counts
        self.say("%d fetched (%d resumed), %d already complete, %d vanished, %d short, %.1f MB" % (c["done"], c["resumed"], c["skipped"], c["vanished"], c["short"], c["bytes"] / 1e6))
        if self.args.check and not self.stop.is_set():
            self.check(rows)
        return c["vanished"] + c["short"]

    def lock_window(self, remaining):
        """PutLock the remaining rows by segno range; on 507 the longest prefix that fits under the cap.
        Returns (window, consumed): rows the tile reports missing are reported and dropped from the window
        but count as consumed."""
        if not self.lock_id:
            self.lock_id = "fetch-%s-%s" % (node_dir(self.node.node_id)[-6:], secrets.token_hex(2))
        window = remaining
        while True:
            sel = pb.Selector(segno=pb.SegnoRange(**{"from": window[0].segno, "to": window[-1].segno}))
            try:
                r = self.c.rpc("PutLock", pb.PutLockRequest(id=self.lock_id, select=sel, reason="orch fetch", requester=self.args.requester))
                break
            except TileError as e:
                if e.status != 507:
                    raise
                d = e.error.detail
                room = int(d.get("lock_budget_bytes", 0)) - int(d.get("locked_bytes", 0))
                fit, total = [], 0
                for s in window:
                    if total + s.bytes > room:
                        break
                    fit.append(s); total += s.bytes
                if not fit:
                    raise Refused("lock refused: %s; not even one segment fits (%d bytes free under the cap). --no-lock copies unprotected" % (e.error.message, room))
                if len(fit) == len(window):
                    raise Refused("lock refused although the window fits: %s" % dict(d))
                window = fit
        if r.created:
            self.lock_created = True
            self._write_manifest(lock=self.lock_id)
        elif not self.lock_created:
            self.say("lock %s already held by %s: joined, not released" % (self.lock_id, r.lock.requester or "?"))
        consumed = len(window)
        if r.missing:
            self.say("%d segments vanished before they could be locked: %s" % (len(r.missing), list(r.missing)))
            self.counts["vanished"] += len(r.missing)
            window = [s for s in window if s.segno not in set(r.missing)]
        if consumed < len(remaining):
            self.say("lock window: %d of %d remaining segments (%.1f MB) under the lock cap" % (consumed, len(remaining), sum(s.bytes for s in window) / 1e6))
        return window, consumed

    def copy(self, window):
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.jobs) as ex:
            futures = {ex.submit(self.download, s): s for s in window}
            try:
                for fut in concurrent.futures.as_completed(futures):
                    s = futures[fut]
                    try:
                        verdict = fut.result()
                    except (OSError, TileError) as e:
                        verdict = "short"
                        self.say("segment %d: %s" % (s.segno, e))
                    self.counts[verdict] += 1
                    if verdict in ("done", "resumed"):
                        self.counts["bytes"] += s.bytes
                        if verdict == "resumed":
                            self.counts["done"] += 1
            except KeyboardInterrupt:
                self.stop.set()
                for f in futures:
                    f.cancel()
                raise

    def download(self, s):
        """One segment through its .part; the resume table of PLAN_ORCH.md §5."""
        if self.stop.is_set():
            return "short"
        final = os.path.join(self.seg_dir, "%012d.h265" % s.segno)
        part = final + ".part"
        if os.path.exists(final):
            if os.path.getsize(final) == s.bytes:
                return "skipped"
            os.unlink(final)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > s.bytes:
            have = 0
        if have == s.bytes:
            os.replace(part, final)
            return "skipped"
        resumed = have > 0
        st, hdr, r, conn = self.c.open("/v1/segments/%d/data" % s.segno, "bytes=%d-" % have if have else None)
        if st == 416:
            conn.close(); have = 0; resumed = False
            st, hdr, r, conn = self.c.open("/v1/segments/%d/data" % s.segno)
        try:
            if st == 404:
                self.say("segment %d vanished from the tile" % s.segno)
                return "vanished"
            if st not in (200, 206):
                raise TileError(st, pb.Error(code=pb.Error.INTERNAL, message=r.read(200).decode(errors="replace")))
            if st == 206:
                total = int(hdr.get("content-range", "/0").rsplit("/", 1)[1])
                if total != s.bytes:
                    self.say("segment %d: tile has %d bytes, listed %d" % (s.segno, total, s.bytes))
                    return "short"
            mode = "ab" if st == 206 and have else "wb"
            with open(part, mode) as f:
                while not self.stop.is_set():
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
        finally:
            conn.close()
        if os.path.getsize(part) != s.bytes:
            return "short"
        os.replace(part, final)
        return "resumed" if resumed else "done"

    def session_logs(self, rows):
        log_dir = os.path.join(self.dir, "logs")
        for sid in sorted({s.session for s in rows if s.session != "recovered"}):
            st, _, data = self.c.get("/v1/sessions/%s/log" % sid)
            if st == 200:
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, sid + ".jsonl"), "wb") as f:
                    f.write(data)

    # ── step 9: --check, the conformance suite's C4 predicates on the laptop ─────────────────────────
    def check(self, rows):
        bad = 0
        for s in rows:
            path = os.path.join(self.seg_dir, "%012d.h265" % s.segno)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                aus = sei.split(f.read())
            recs = [sei.record_of(n) for _, _, n in aus]
            problems = []
            if len(aus) != s.frames:
                problems.append("%d AUs, listed %d frames" % (len(aus), s.frames))
            if not all(recs):
                problems.append("%d AUs without SEI" % recs.count(None))
            for r0, r1 in zip(recs, recs[1:]):
                if r0 and r1 and r0[0] == r1[0] and not (r1[4] & 0x11) and r1[1] != r0[1] + 1:
                    problems.append("K %d -> %d" % (r0[1], r1[1])); break
            if problems:
                bad += 1
                self.say("check: segment %d: %s" % (s.segno, "; ".join(problems)))
        self.say("check: %d files, %d with findings" % (len(rows), bad))
        return bad


def main(args, between_list_and_copy=None):
    hosts = load_nodes(args.nodes, args.config)
    failures = 0
    for host in hosts:
        c = Client(host)
        say = lambda m, h=c.host: print("%-22s %s" % (h, m), flush=True)  # noqa: E731
        try:
            failures += Node(c, args.out, args, say, between_list_and_copy).run()
        except Refused as e:
            say("refused: %s" % e); failures += 1
        except TileError as e:
            say(str(e)); failures += 1
        except OSError as e:
            say("unreachable: %s" % e); failures += 1
        except KeyboardInterrupt:
            return 130
    return 1 if failures else 0

