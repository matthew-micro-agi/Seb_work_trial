"""tile.v1 conformance suite (DESIGN_RING_CONTROL.md §8.5), run from the bench PC against a node.

    python3 icd/conformance.py HOST[:PORT] [--segment-s 2] [--kill-recorder CMD] [--restart-agent CMD] [--fsck CMD]
                               [--only C3,C4] [--wrap-timeout 900]

Every reply is parsed through the generated class for its method; a field of the wrong type fails the check.
The node under test should run with a small budget (--budget 10M) so C7 wraps the ring in minutes.
Checks that need a hand on the node take a shell command and report SKIP, never PASS, without it:
  --kill-recorder   kill -9 the recorder mid-segment      PC: 'pkill -9 -x kpipe-synth'      board: 'ssh evm pkill -9 -x kpipe'
  --restart-agent   stop the agent, delete index.db, start it again (TESTING.md has the PC and board lines)
  --fsck            run ringfsck on the node's ring        PC: 'segfile/build/ringfsck /dev/shm/ring'
Exit status 1 if any check fails.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "py"))
sys.path.insert(0, os.path.join(HERE, "..", "sei", "python"))
import tilev1 as pb  # noqa: E402
import sei  # noqa: E402


class Fail(Exception):
    pass


class Skip(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Node:
    def __init__(self, host):
        self.host = host
        self.node = None

    def http(self, method, path, body=None, headers=None, stream_ms=0):
        """stream_ms > 0: read for that long and hang up, for an endpoint that never ends (/v1/live)."""
        c = http.client.HTTPConnection(self.host, timeout=60)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        if stream_ms and r.status == 200:
            deadline = time.monotonic() + stream_ms / 1000.0
            chunks = []
            while time.monotonic() < deadline:
                d = r.read1(1 << 16)      # read1: a chunked stream must not wait for a full buffer
                if not d:
                    break
                chunks.append(d)
            data = b"".join(chunks)
        else:
            data = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data

    def rpc(self, name, req=None, expect=200):
        """Parsed reply (through the generated class) on `expect`; otherwise raises Fail unless expect is None,
        in which case (status, Error-or-reply) is returned."""
        req_cls, rep_cls = pb.METHODS[name]
        body = pb.to_json(req if req is not None else req_cls()).encode()
        st, _, data = self.http("POST", "/tile.v1.Tile/" + name, body, {"Content-Type": "application/json"})
        if st == 200:
            try:
                msg = pb.from_json(data.decode(), rep_cls)
            except Exception as e:  # noqa: BLE001
                raise Fail("%s: reply does not parse as %s: %s" % (name, rep_cls.__name__, e))
        else:
            try:
                msg = pb.from_json(data.decode(), pb.Error)
            except Exception as e:  # noqa: BLE001
                raise Fail("%s: %d with a body that does not parse as Error: %s" % (name, st, e))
        if expect is None:
            return st, msg
        if st != expect:
            raise Fail("%s: expected %d, got %d %s" % (name, expect, st, data[:200].decode(errors="replace")))
        return msg

    def has(self, cap):
        return cap in self.node.capabilities

    def fetch(self, segno, rng=None):
        h = {"Range": rng} if rng else {}
        return self.http("GET", "/v1/segments/%d/data" % segno, headers=h)

    def health(self):
        return self.rpc("GetHealth")


def sel_last(s): return pb.ListSegmentsRequest(select=pb.Selector(last_s=s))
def sel_segno(a, b): return pb.Selector(segno=pb.SegnoRange(**{"from": a, "to": b}))
def sel_k(e, a, b): return pb.Selector(k=pb.KRange(epoch=e, k_from=a, k_to=b))
def sel_ptp(a, b): return pb.Selector(ptp=pb.PtpInterval(since_ns=a, until_ns=b))


def au_records(data):
    aus = sei.split(data)
    recs = [sei.record_of(nals) for _, _, nals in aus]
    return aus, recs


class Suite:
    def __init__(self, a):
        self.a = a
        self.n = Node(a.host)
        self.results = []
        self.footage = []       # Segment messages from C3
        self.lock_id = "conf-%d" % int(time.time())
        self.locked = {}        # segno -> bytes fetched right after locking
        self.recording = False

    def run(self, name, fn):
        if self.a.only and name not in self.a.only:
            return
        t0 = time.time()
        try:
            note = fn() or ""
            self.results.append((name, "PASS", note))
        except Skip as e:
            self.results.append((name, "SKIP", str(e)))
        except Fail as e:
            self.results.append((name, "FAIL", str(e)))
        except Exception as e:  # noqa: BLE001
            self.results.append((name, "FAIL", "%s: %r" % (fn.__doc__ or name, e)))
        r = self.results[-1]
        print("%-4s %-4s %s%s" % (r[0], r[1], r[2], " (%.0f s)" % (time.time() - t0) if time.time() - t0 >= 2 else ""), flush=True)

    # ── checks ───────────────────────────────────────────────────────────────────────────────────────
    def c1(self):
        """GetNode parses; api_version 1.x; every capability known"""
        n = self.n.rpc("GetNode")
        check(n.api_version.startswith("1."), "api_version %r" % n.api_version)
        unknown = [c for c in n.capabilities if c not in pb.CAPABILITIES]
        check(not unknown, "unknown capabilities %s" % unknown)
        check("core" in n.capabilities, "core missing")
        self.n.node = n
        return "%s hw=%s fw=%s caps=%s" % (n.node_id, n.hw, n.firmware, ",".join(n.capabilities))

    def c2(self):
        """GetHealth and GetTime parse; ptp_ns increases; offset stable < 1 ms"""
        h = self.n.health()
        t1 = self.n.rpc("GetTime"); time.sleep(0.2); t2 = self.n.rpc("GetTime")
        check(t2.ptp_ns > t1.ptp_ns, "ptp_ns did not increase: %d -> %d" % (t1.ptp_ns, t2.ptp_ns))
        check(abs(t2.ptp_minus_mono_ns - t1.ptp_minus_mono_ns) < 1_000_000, "offset moved by %d ns" % (t2.ptp_minus_mono_ns - t1.ptp_minus_mono_ns))
        check(t1.boot_id, "boot_id empty")
        return "state=%s faults=%s locked=%s" % (pb.RecordingStatus.State.Name(h.recording.state), [pb.NodeFault.Name(f) for f in h.faults_active], t1.ptp_locked)

    def c3(self):
        """StartRecording (grid); after 3 x segment_s, last_s = 2 x segment_s lists >= 1 segment with frames > 0"""
        s = self.a.segment_s
        if not self.n.has("recording"):
            lst = self.n.rpc("ListSegments", sel_last(1e9))
            check(lst.segments, "no recording capability and no footage on the node")
            self.footage = list(lst.segments)
            raise Skip("recording not declared; C4-C10 use the %d segments already on the node" % len(self.footage))
        st = self.n.rpc("GetRecording")
        if st.state != pb.RecordingStatus.IDLE:
            self.n.rpc("StopRecording")
        req = pb.StartRecordingRequest(edges=pb.StartRecordingRequest.GRID, latency_ns=20_000_000, segment_s=s)
        st = self.n.rpc("StartRecording", req)
        check(st.state == pb.RecordingStatus.RUNNING and st.session, "state after start %s" % pb.RecordingStatus.State.Name(st.state))
        self.recording = True
        st2, _ = self.n.rpc("StartRecording", req, expect=None)
        check(st2 == 409, "second StartRecording answered %d, not 409" % st2)
        deadline = time.time() + 3 * s + 15
        while time.time() < deadline:
            time.sleep(1)
            lst = self.n.rpc("ListSegments", sel_last(2 * s))
            if len(lst.segments) >= 2:
                break
        check(len(lst.segments) >= 1, "no segment after %d s" % (3 * s + 15))
        for seg in lst.segments:
            check(seg.frames > 0 and seg.bytes > 0, "segment %d frames=%d bytes=%d" % (seg.segno, seg.frames, seg.bytes))
            check(not seg.HasField("first") or seg.last.k >= seg.first.k, "segment %d k_last < k_first" % seg.segno)
        self.footage = list(lst.segments)
        return "session %s, %d segments, newest %d (%d frames)" % (st.session, len(lst.segments), lst.segments[-1].segno, lst.segments[-1].frames)

    @staticmethod
    def unexplained(seg):
        """Segment.faults minus the bits that are statements rather than errors: K_LOCAL says this
        footage was never anchored to a trigger source, which is normal on a node without a beacon."""
        return [f for f in seg.faults if f != pb.K_LOCAL]

    def c4(self):
        """fetch: length = bytes; VPS first; first VCL is IDR; SEI on every AU; K +1; K chain across segments"""
        check(self.footage, "no footage (C3)")
        prev = None
        for seg in self.footage[-2:]:
            st, hdr, data = self.n.fetch(seg.segno)
            check(st == 200, "GET data: %d" % st)
            check(len(data) == seg.bytes, "segment %d: body %d bytes, listed %d" % (seg.segno, len(data), seg.bytes))
            aus, recs = au_records(data)
            # The Wave5 puts an access unit delimiter (35) in front of every access unit but the very
            # first of a stream, so a segment that opens at a later IDR opens with the AUD and the VPS
            # comes next. §10's rule is about the parameter sets, not the byte offset.
            head = [n for n in aus[0][2] if sei.nal_type(n) != 35]
            check(head and sei.nal_type(head[0]) == 32,
                  "segment %d: first NAL types %s, expected VPS (32) after any AUD (35)"
                  % (seg.segno, [sei.nal_type(n) for n in aus[0][2][:4]]))
            vcl = [n for n in aus[0][2] if sei.is_vcl(n)]
            check(vcl and sei.is_idr(vcl[0]), "segment %d: first VCL NAL is not an IDR" % seg.segno)
            check(all(recs), "segment %d: %d AUs without SEI" % (seg.segno, recs.count(None)))
            check(len(aus) == seg.frames, "segment %d: %d AUs, listed frames %d" % (seg.segno, len(aus), seg.frames))
            for r0, r1 in zip(recs, recs[1:]):
                # DUPLICATE, UNMATCHED, EPOCH_CHANGE, K_CORRECTED: the frame explains its own step in K
                if r0[0] == r1[0] and not (r1[4] & 0x131):
                    check(r1[1] == r0[1] + 1, "segment %d: K %d -> %d" % (seg.segno, r0[1], r1[1]))
            if (prev is not None and prev.segno + 1 == seg.segno and not self.unexplained(prev) and not self.unexplained(seg)
                    and prev.HasField("last") and seg.HasField("first")):
                check(seg.first.k == prev.last.k + 1, "K chain %d -> %d between segments %d and %d" % (prev.last.k, seg.first.k, prev.segno, seg.segno))
            prev = seg
        return "%d segments fetched and parsed" % len(self.footage[-2:])

    def c5(self):
        """Range: bytes=100-199 equals bytes 100..199 of the full fetch"""
        check(self.footage, "no footage (C3)")
        seg = self.footage[-1]
        _, _, full = self.n.fetch(seg.segno)
        st, hdr, part = self.n.fetch(seg.segno, "bytes=100-199")
        check(st == 206, "Range answered %d" % st)
        check(part == full[100:200], "range bytes differ")
        check(hdr.get("content-range", "").startswith("bytes 100-199/"), "Content-Range %r" % hdr.get("content-range"))

    def c6(self):
        """the same footage by segno, by (epoch, k) and by PTP time gives the same segno set"""
        seg = next((s for s in reversed(self.footage) if s.HasField("first") and not self.unexplained(s)), None)
        check(seg is not None, "no fault-free matched segment in the footage")
        by_segno = [s.segno for s in self.n.rpc("ListSegments", pb.ListSegmentsRequest(select=sel_segno(seg.segno, seg.segno))).segments]
        by_k = [s.segno for s in self.n.rpc("ListSegments", pb.ListSegmentsRequest(select=sel_k(seg.first.epoch, seg.first.k, seg.last.k))).segments]
        by_ptp = [s.segno for s in self.n.rpc("ListSegments", pb.ListSegmentsRequest(select=sel_ptp(seg.t_ptp_first_ns, seg.t_ptp_last_ns))).segments]
        check(by_segno == by_k == by_ptp == [seg.segno], "segno %s, k %s, ptp %s" % (by_segno, by_k, by_ptp))
        return "segment %d by segno, K %d..%d and PTP" % (seg.segno, seg.first.k, seg.last.k)

    def c7(self):
        """lock last_s; after oldest_segno has passed the locked segnos twice they still fetch identical, reclaimable false, others 404"""
        check(self.footage, "no footage (C3)")
        if not self.recording:
            raise Skip("no recording running: the ring cannot wrap")
        h0 = self.n.health()
        depth = max(h0.ring.segments, 1)
        req = pb.PutLockRequest(id=self.lock_id, select=pb.Selector(last_s=2 * self.a.segment_s), reason="conformance C7", requester="conformance.py")
        r = self.n.rpc("PutLock", req)
        check(r.created and r.lock.segnos and not r.missing, "PutLock: created=%s segnos=%s missing=%s" % (r.created, list(r.lock.segnos), list(r.missing)))
        for s in r.lock.segnos:
            self.locked[s] = self.n.fetch(s)[2]
        unlocked = min(r.lock.segnos) - 1
        target = max(r.lock.segnos) + 2 * depth
        deadline = time.time() + self.a.wrap_timeout
        while time.time() < deadline:
            h = self.n.health()
            if h.ring.oldest_segno > target:
                break
            check(pb.STORAGE_FULL not in h.faults_active, "STORAGE_FULL raised while waiting for the wrap")
            time.sleep(2)
        check(h.ring.oldest_segno > target, "ring did not pass segno %d in %d s (oldest %d); budget too large?" % (target, self.a.wrap_timeout, h.ring.oldest_segno))
        for s, before in self.locked.items():
            st, _, data = self.n.fetch(s)
            check(st == 200 and data == before, "locked segment %d changed or vanished (%d)" % (s, st))
            seg = self.n.rpc("GetSegment", pb.SegnoRequest(segno=s))
            check(not seg.reclaimable and self.lock_id in seg.locks, "segment %d reclaimable=%s locks=%s" % (s, seg.reclaimable, list(seg.locks)))
        if unlocked > 0:
            st, _, _ = self.n.fetch(unlocked)
            check(st == 404, "unlocked segment %d still fetches (%d)" % (unlocked, st))
        total = sum(len(b) for b in self.locked.values())
        check(h.ring.locked_bytes == total, "locked_bytes %d, sum of locked files %d" % (h.ring.locked_bytes, total))
        return "%d segments locked, ring wrapped past %d, %d bytes locked" % (len(self.locked), target, total)

    def c8(self):
        """second PutLock on the same id with a newer selector: created false, added = new segnos only, union"""
        if not self.locked:
            r = self.n.rpc("PutLock", pb.PutLockRequest(id=self.lock_id, select=sel_segno(self.footage[0].segno, self.footage[0].segno)))
            for s in r.lock.segnos:
                self.locked[s] = b""
        before = set(self.locked)
        r = self.n.rpc("PutLock", pb.PutLockRequest(id=self.lock_id, select=pb.Selector(last_s=self.a.segment_s)))
        check(not r.created, "created should be false")
        check(set(r.added).isdisjoint(before), "added contains old members %s" % (set(r.added) & before))
        check(set(r.lock.segnos) == before | set(r.added), "segnos %s != union" % list(r.lock.segnos))
        for s in r.added:
            self.locked[s] = b""
        return "added %s" % list(r.added)

    def c9(self):
        """a lock larger than the cap: 507, no lock created, locked_bytes unchanged"""
        h0 = self.n.health()
        st, err = self.n.rpc("PutLock", pb.PutLockRequest(id="conf-toolarge", select=sel_segno(0, 1 << 60)), expect=None)
        if st == 200:
            self.n.rpc("DeleteLock", pb.LockId(id="conf-toolarge"))
            raise Skip("everything on the node fits under the lock cap; raise the footage or lower --lock-budget")
        check(st == 507 and err.code == pb.Error.INSUFFICIENT_STORAGE, "expected 507 INSUFFICIENT_STORAGE, got %d %s" % (st, err))
        check("conf-toolarge" not in [l.id for l in self.n.rpc("ListLocks").locks], "lock directory created despite 507")
        h1 = self.n.health()
        check(h1.ring.locked_bytes == h0.ring.locked_bytes, "locked_bytes changed %d -> %d" % (h0.ring.locked_bytes, h1.ring.locked_bytes))
        return "507 detail %s" % dict(err.detail)

    def c10(self):
        """DeleteLock: former members out of the ring answer 404; locked_bytes 0; used_bytes drops"""
        check(self.locked, "nothing locked (C7/C8)")
        h0 = self.n.health()
        gone = [s for s in self.locked if not self.n.rpc("GetSegment", pb.SegnoRequest(segno=s)).reclaimable]
        freed = sum(self.n.rpc("GetSegment", pb.SegnoRequest(segno=s)).bytes for s in gone)
        self.n.rpc("DeleteLock", pb.LockId(id=self.lock_id))
        st, _ = self.n.rpc("DeleteLock", pb.LockId(id=self.lock_id), expect=None)
        check(st == 404, "second DeleteLock answered %d" % st)
        for s in gone:
            check(self.n.fetch(s)[0] == 404, "segment %d still fetches after DeleteLock" % s)
            check(self.n.rpc("GetSegment", pb.SegnoRequest(segno=s), expect=None)[0] == 404, "GetSegment %d not 404" % s)
        h1 = self.n.health()
        check(h1.ring.locked_bytes == 0, "locked_bytes %d after DeleteLock" % h1.ring.locked_bytes)
        if gone:
            slack = 2 * max(s.bytes for s in self.footage)
            check(h1.ring.used_bytes <= h0.ring.used_bytes - freed + slack, "used_bytes %d -> %d, expected a drop of ~%d" % (h0.ring.used_bytes, h1.ring.used_bytes, freed))
        self.locked = {}
        return "%d members were out of the ring, %d bytes freed" % (len(gone), freed)

    def c11(self):
        """kill -9 the recorder mid-segment: respawned within 10 s; the segment shows partial with frames = its AU count"""
        if not self.recording:
            raise Skip("no recording running")
        if not self.a.kill_recorder:
            raise Skip("no --kill-recorder command")
        open_segno = 0
        for _ in range(10):
            open_segno = self.n.rpc("GetRecording").open_segno
            if open_segno:
                break
            time.sleep(0.5)
        check(open_segno, "open_segno never reported")
        time.sleep(0.6)                      # be inside the segment, not at its first AU
        subprocess.run(self.a.kill_recorder, shell=True, check=False)
        deadline = time.time() + 15
        seg = None
        while time.time() < deadline:            # open_segno came from a 1 Hz status line: look a little past it
            time.sleep(1)
            h = self.n.health()
            lst = self.n.rpc("ListSegments", pb.ListSegmentsRequest(select=sel_segno(open_segno, open_segno + 3)), expect=None)
            if lst[0] == 200 and h.recording.state == pb.RecordingStatus.RUNNING and pb.RECORDER_DOWN not in h.faults_active:
                seg = next((s for s in lst[1].segments if s.partial), None)
                if seg is not None:
                    break
        check(seg is not None, "no partial segment among %d..%d within 15 s (state %s, faults %s)" % (open_segno, open_segno + 3, pb.RecordingStatus.State.Name(h.recording.state), [pb.NodeFault.Name(f) for f in h.faults_active]))
        aus, _ = au_records(self.n.fetch(seg.segno)[2])
        check(len(aus) == seg.frames, "partial segment %d: %d AUs, frames %d" % (seg.segno, len(aus), seg.frames))
        return "segment %d partial, %d frames" % (seg.segno, seg.frames)

    def c12(self):
        """stop, delete index.db, restart the agent: the list equals the snapshot"""
        if not self.a.restart_agent:
            if self.recording:
                self.n.rpc("StopRecording"); self.recording = False
            raise Skip("no --restart-agent command")
        if self.recording:
            st = self.n.rpc("StopRecording")
            check(st.state == pb.RecordingStatus.IDLE, "state after stop %s" % pb.RecordingStatus.State.Name(st.state))
            self.recording = False
        before = self.n.rpc("ListSegments", sel_last(1e9)).segments
        locks_before = [(l.id, list(l.segnos)) for l in self.n.rpc("ListLocks").locks]
        subprocess.run(self.a.restart_agent, shell=True, check=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(1)
            try:
                h = self.n.health()
            except (OSError, Fail):
                continue
            if pb.INDEX_REBUILDING not in h.faults_active:
                break
        check(pb.INDEX_REBUILDING not in h.faults_active, "index still rebuilding after 180 s")
        after = self.n.rpc("ListSegments", sel_last(1e9)).segments
        key = lambda s: (s.segno, s.first.epoch, s.first.k, s.last.epoch, s.last.k, s.t_ptp_first_ns, s.t_ptp_last_ns, s.frames, s.bytes, tuple(s.faults), tuple(s.locks))  # noqa: E731
        a, b = [key(s) for s in before], [key(s) for s in after]
        check(a == b, "list differs after rebuild: %d vs %d rows, first difference %s" % (len(a), len(b), next(((x, y) for x, y in zip(a, b) if x != y), None)))
        check(locks_before == [(l.id, list(l.segnos)) for l in self.n.rpc("ListLocks").locks], "locks differ after restart")
        return "%d segments identical after rebuild" % len(after)

    def c13(self):
        """every method and endpoint answers its codes or 501, 501 only when the capability is absent; 415 for application/proto"""
        cap_of = {"GetNode": "core", "GetHealth": "core", "GetTelemetry": "core", "GetTime": "core", "GetConfig": "core",
                  "SetSensor": "config.live_sensor", "GetTrigger": "trigger", "SetTrigger": "trigger",
                  "GetRecording": "recording", "StartRecording": "recording", "StopRecording": "recording",
                  "ListSegments": "segments", "GetSegment": "segments", "ListSessions": "sessions",
                  "ListLocks": "locks", "GetLock": "locks", "PutLock": "locks", "DeleteLock": "locks"}
        allowed = {200, 400, 404, 409, 503, 507}
        notes = []
        # SetTrigger is called below with a default request, and `armed` is a proto3 bool with no
        # presence: on a node that generates the pulse that stops the train, and everything after this
        # check would record no frames. Put back whatever was there.
        trigger_was = None
        if self.n.has("trigger.local") or self.n.has("trigger.external"):
            trigger_was = self.n.rpc("GetTrigger").settings
        for name, cap in cap_of.items():
            if name == "StartRecording" and self.n.has("recording"):
                continue                      # would start a session
            declared = self.n.has(cap) or (cap == "trigger" and (self.n.has("trigger.local") or self.n.has("trigger.external")))
            if name == "SetTrigger" and declared:
                continue                      # an empty TriggerSettings is armed=false: it would stop the pulses,
                #                               and with them the camera, the trigger log and the rest of the suite
            st, msg = self.n.rpc(name, expect=None)
            if declared:
                check(st in allowed, "%s: %d not in %s" % (name, st, sorted(allowed)))
            else:
                check(st == 501 and msg.code == pb.Error.NOT_IMPLEMENTED, "%s: capability absent, expected 501 NOT_IMPLEMENTED, got %d %s" % (name, st, msg))
                notes.append(name)
        for path, cap in (("/v1/live", "live"), ("/v1/trigger/log", "trigger.log"), ("/metrics", "metrics"),
                          ("/v1/segments/999999999999/data", "segments"), ("/v1/sessions/no-such-session/log", "sessions")):
            st, _, data = self.n.http("GET", path, stream_ms=200 if path == "/v1/live" else 0)
            if self.n.has(cap):
                # /v1/live is 503 while nothing is recording: the branch lives inside the recorder (§8.3a)
                check(st in (200, 404, 503), "%s: %d" % (path, st))
            else:
                check(st == 501, "%s: expected 501, got %d" % (path, st))
                pb.from_json(data.decode(), pb.Error)
        if trigger_was is not None:
            self.n.rpc("SetTrigger", trigger_was)
            check(self.n.rpc("GetTrigger").settings.armed == trigger_was.armed, "the trigger was not restored after SetTrigger")
        st, _, data = self.n.http("POST", "/tile.v1.Tile/GetNode", b"\x00", {"Content-Type": "application/proto"})
        check(st in (200, 415), "application/proto answered %d" % st)
        if st == 415:
            check(pb.from_json(data.decode(), pb.Error).code == pb.Error.UNSUPPORTED_MEDIA_TYPE, "415 body code")
        return "501 on %s; binary codec %s" % (",".join(notes) or "nothing", "served" if st == 200 else "415")

    def c14(self):
        """south contract: ringfsck reports nothing to fix"""
        if self.recording:
            self.n.rpc("StopRecording"); self.recording = False
        if not self.a.fsck:
            raise Skip("no --fsck command")
        r = subprocess.run(self.a.fsck, shell=True, capture_output=True, text=True)
        check(r.returncode == 0, "ringfsck exit %d:\n%s%s" % (r.returncode, r.stdout, r.stderr))
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "ok"

    def c15(self):
        """/metrics parses and fps_in matches GetTelemetry; /v1/live from an IDR with K + 1 per AU"""
        notes = []
        if self.n.has("metrics"):
            t = self.n.rpc("GetTelemetry")
            st, _, data = self.n.http("GET", "/metrics")
            check(st == 200, "/metrics %d" % st)
            vals = {}
            for line in data.decode().splitlines():
                if line and not line.startswith("#"):
                    name, val = line.rsplit(" ", 1)
                    vals[name.split("{")[0]] = float(val)
            check("tile_fps_in" in vals, "tile_fps_in missing from /metrics")
            check(abs(vals["tile_fps_in"] - t.fps_in) <= max(t.fps_in, 1), "fps_in %s vs telemetry %d" % (vals["tile_fps_in"], t.fps_in))
            notes.append("/metrics %d series" % len(vals))
        if self.n.has("live"):
            # The live view comes out of the recorder, so it answers 503 while nothing is recording
            # (§8.3a): check that, then record and read the stream. C13 leaves the node idle.
            if self.n.has("recording"):
                if self.n.rpc("GetRecording").state != pb.RecordingStatus.IDLE:
                    self.n.rpc("StopRecording")
                self.recording = False
            if not self.recording:
                st, _, _ = self.n.http("GET", "/v1/live", stream_ms=200)
                check(st == 503, "/v1/live while idle: expected 503, got %d" % st)
                notes.append("/v1/live 503 while idle")
                if self.n.has("recording"):
                    self.n.rpc("StartRecording", pb.StartRecordingRequest(edges=pb.StartRecordingRequest.GRID,
                                                                          latency_ns=33_360_000, segment_s=self.a.segment_s))
                    self.recording = True
                    # No frames means no live view, and the cause is upstream of anything /v1/live can
                    # say: an unarmed trigger, or a camera that did not open.
                    deadline = time.time() + 25
                    while time.time() < deadline and self.n.rpc("GetTelemetry").fps_in == 0:
                        time.sleep(1)
                    check(self.n.rpc("GetTelemetry").fps_in > 0,
                          "recording started but no frames are arriving: nothing to proxy (trigger armed? camera free?)")
            if self.recording:
                st, hdr, data = self.n.http("GET", "/v1/live", stream_ms=10_000)
                check(st == 200, "/v1/live %d" % st)
                aus = sei.split(data, "h264")
                check(len(aus) >= 2, "/v1/live gave %d access units in 10 s" % len(aus))
                head = aus[0][2]
                check(any(sei.is_first_param_set(n, "h264") for n in head) and any(sei.is_idr(n, "h264") for n in head),
                      "the first access unit must be SPS/PPS + IDR, got NAL types %s" % [sei.nal_type(n, "h264") for n in head])
                recs = [sei.record_of(n, "h264") for _, _, n in aus[:-1]]
                check(all(recs), "%d of %d access units carry no camsync SEI" % (recs.count(None), len(recs)))
                ks = [r[1] for r in recs if r]
                steps = sorted(set(b - a for a, b in zip(ks, ks[1:])))
                check(steps and min(steps) >= 1, "K must increase along the live stream, steps seen %s" % steps)
                fps = self.n.rpc("GetConfig").proxy.fps or 15
                check(len(aus) >= 5 * fps, "%d access units in 10 s at a declared %d fps" % (len(aus), fps))
                rate = len(data) * 8 / 10_000
                notes.append("/v1/live %d AUs, K %d..%d step %s, %.0f kbps, live_clients %d"
                             % (len(aus), ks[0], ks[-1], steps, rate,
                                self.n.rpc("GetRecording").live_clients))
        if not notes:
            raise Skip("neither metrics nor live declared")
        return "; ".join(notes)

    def c16(self):
        """trigger.log: the newest line's (epoch, k) equals GetTrigger within one second of pulses"""
        if not self.n.has("trigger.log"):
            raise Skip("trigger.log not declared")
        st, _, data = self.n.http("GET", "/v1/trigger/log")
        check(st == 200, "/v1/trigger/log %d" % st)
        last = json.loads(data.decode().strip().splitlines()[-1])
        t = self.n.rpc("GetTrigger")
        period_pulses = round(1e9 / (t.settings.period_ns or 33_333_333))
        check(last["epoch"] == t.epoch and abs(int(last["k"]) - t.k) <= period_pulses, "log %s vs GetTrigger epoch %d k %d" % (last, t.epoch, t.k))

    def main(self):
        for name, fn in (("C1", self.c1), ("C2", self.c2), ("C3", self.c3), ("C4", self.c4), ("C5", self.c5), ("C6", self.c6), ("C7", self.c7),
                         ("C8", self.c8), ("C9", self.c9), ("C10", self.c10), ("C11", self.c11), ("C13", self.c13), ("C15", self.c15),
                         ("C16", self.c16), ("C12", self.c12), ("C14", self.c14)):
            if name == "C1" or self.n.node is not None:
                self.run(name, fn)
        if self.recording:
            try:
                self.n.rpc("StopRecording")
            except Fail:
                pass
        counts = {k: sum(1 for r in self.results if r[1] == k) for k in ("PASS", "FAIL", "SKIP")}
        print("conformance %s: %d passed, %d failed, %d skipped" % (self.a.host, counts["PASS"], counts["FAIL"], counts["SKIP"]))
        return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help="HOST[:PORT], default port 8080")
    p.add_argument("--segment-s", type=int, default=2, help="segment length requested for the test session")
    p.add_argument("--kill-recorder", help="shell command that kill -9s the recorder on the node")
    p.add_argument("--restart-agent", help="shell command that stops the agent, deletes index.db and starts it again")
    p.add_argument("--fsck", help="shell command that runs ringfsck on the node's ring")
    p.add_argument("--only", help="comma-separated checks, e.g. C1,C2,C13 (C1 always runs)")
    p.add_argument("--wrap-timeout", type=int, default=900, help="seconds to wait for C7's two wraps")
    a = p.parse_args()
    if ":" not in a.host:
        a.host += ":8080"
    a.only = set(a.only.split(",")) | {"C1"} if a.only else None
    sys.exit(Suite(a).main())
