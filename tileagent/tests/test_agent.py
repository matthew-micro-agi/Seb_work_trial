"""tile-agent unit tests: index_segment and the selectors on a fixture ring, the lock map, reconcile, the
supervisor's state machine with a scripted recorder, and the live-view fan-out against a synthetic proxy
socket. The HTTP surface is covered by icd/conformance.py.

    RING_FIXTURE=/dev/shm/ring-fixture python3 -m unittest tileagent.tests.test_agent     (from the repo root)

RING_FIXTURE is produced by segfile/tests/fixture.sh; without it the index tests are skipped.
"""
import argparse
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from tileagent import live, supervisor  # noqa: E402
from tileagent.agent import Agent, ApiError  # noqa: E402
from tileagent.index import Index, index_segment  # noqa: E402
from tileagent.locks import Locks  # noqa: E402
from tileagent.pb import pb, sei as seitwin  # noqa: E402

FIXTURE = os.environ.get("RING_FIXTURE")


def args(ring, **kw):
    d = dict(ring=ring, budget=0, lock_budget=0, segment_s=2, fps=30, width=1456, height=1088, exposure=1104, gain=0, bitrate=2_000_000,
             recorder=None, no_recorder=True, trigger_sh="/nonexistent", ptp="/nonexistent", hw="pc", node_id="test",
             proxy=False, proxy_sock=None, proxy_width=728, proxy_height=544, proxy_fps=15, proxy_bitrate=1_000_000,
             beacon_src=None, no_trigger_log=False)
    d.update(kw)
    return argparse.Namespace(**d)


def copy_ring(src):
    dst = tempfile.mkdtemp(prefix="ring-", dir=os.environ.get("TMPDIR", "/dev/shm"))
    shutil.copytree(os.path.join(src, "seg"), os.path.join(dst, "seg"))
    return dst


def wait_for(cond, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(FIXTURE, "RING_FIXTURE not set")
class IndexTest(unittest.TestCase):
    def setUp(self):
        self.ring = copy_ring(FIXTURE)
        self.agent = Agent(args(self.ring), say=lambda m: None)
        self.assertTrue(wait_for(lambda: not self.agent.rebuilding))

    def tearDown(self):
        self.agent.shutdown()
        shutil.rmtree(self.ring)

    def test_index_segment_matches_file(self):
        seg = sorted(os.listdir(os.path.join(self.ring, "seg")))[0]
        row = index_segment(os.path.join(self.ring, "seg", seg), int(seg[:12]), "s", lambda t: 7)
        self.assertEqual(row["frames"], 60)
        self.assertEqual(row["k_last"], row["k_first"] + 59)
        self.assertEqual(row["ptp_minus_mono"], 7)
        self.assertEqual(row["bytes"], os.path.getsize(os.path.join(self.ring, "seg", seg)))

    def test_rebuild_equals_files(self):
        files = sorted(int(n[:12]) for n in os.listdir(os.path.join(self.ring, "seg")))
        self.assertEqual(sorted(self.agent.index.all_segnos()), files)
        lst = self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(last_s=1e9)))
        self.assertEqual([s.segno for s in lst.segments], files)
        self.assertTrue(all(s.reclaimable and s.session == "recovered" for s in lst.segments))

    def test_selectors_agree(self):
        lst = self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(last_s=1e9))).segments
        # a whole-epoch segment; every segment of the fixture ring is K_LOCAL (no beacon on the PC)
        seg = [s for s in lst if pb.EPOCH_CHANGE not in s.faults][3]
        by_k = self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(k=pb.KRange(epoch=seg.first.epoch, k_from=seg.first.k, k_to=seg.last.k)))).segments
        by_ptp = self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(ptp=pb.PtpInterval(since_ns=seg.t_ptp_first_ns, until_ns=seg.t_ptp_last_ns)))).segments
        self.assertEqual([s.segno for s in by_k], [seg.segno])
        self.assertEqual([s.segno for s in by_ptp], [seg.segno])
        # the epoch-split segment is found by both epochs' K ranges
        split = [s for s in lst if s.first.epoch != s.last.epoch][0]
        self.assertIn(split.segno, [s.segno for s in self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(k=pb.KRange(epoch=2, k_from=0, k_to=0)))).segments])
        self.assertIn(pb.EPOCH_CHANGE, split.faults)
        with self.assertRaises(ApiError):
            self.agent.ListSegments(pb.ListSegmentsRequest())

    def test_locks_survive_reclaim_and_reconcile_drops_rows(self):
        lst = self.agent.ListSegments(pb.ListSegmentsRequest(select=pb.Selector(last_s=1e9))).segments
        a, b = lst[0].segno, lst[1].segno
        r = self.agent.PutLock(pb.PutLockRequest(id="L", select=pb.Selector(segno=pb.SegnoRange(**{"from": a, "to": a})), reason="r", requester="q"))
        self.assertTrue(r.created and list(r.added) == [a])
        r = self.agent.PutLock(pb.PutLockRequest(id="L", select=pb.Selector(segno=pb.SegnoRange(**{"from": a, "to": b}))))
        self.assertFalse(r.created); self.assertEqual(list(r.added), [b]); self.assertEqual(list(r.lock.segnos), [a, b])
        # the recorder reclaims both from seg/
        for s in (a, b):
            os.unlink(os.path.join(self.ring, "seg", "%012d.h265" % s))
        self.agent.reconcile()
        seg = self.agent.GetSegment(pb.SegnoRequest(segno=a))
        self.assertFalse(seg.reclaimable); self.assertEqual(list(seg.locks), ["L"])
        self.assertEqual(self.agent.segment_path(a), os.path.join(self.ring, "lock", "L", "%012d.h265" % a))
        self.assertEqual(self.agent.GetHealth(None).ring.locked_bytes, lst[0].bytes + lst[1].bytes)
        self.agent.DeleteLock(pb.LockId(id="L"))
        with self.assertRaises(ApiError) as cm:
            self.agent.GetSegment(pb.SegnoRequest(segno=a))
        self.assertEqual(cm.exception.status, 404)
        self.assertFalse(os.path.exists(os.path.join(self.ring, "lock", "L")))
        # a lock map rebuilt from disk after a restart
        self.agent.PutLock(pb.PutLockRequest(id="M", select=pb.Selector(segno=pb.SegnoRange(**{"from": lst[2].segno, "to": lst[2].segno}))))
        self.assertEqual(Locks(self.ring).segnos("M"), {lst[2].segno})

    def test_lock_cap(self):
        self.agent.shutdown()
        self.agent = Agent(args(self.ring, budget=20_000_000, lock_budget=600_000), say=lambda m: None)
        self.assertTrue(wait_for(lambda: not self.agent.rebuilding))
        with self.assertRaises(ApiError) as cm:
            self.agent.PutLock(pb.PutLockRequest(id="big", select=pb.Selector(last_s=1e9)))
        self.assertEqual(cm.exception.status, 507)
        self.assertFalse(os.path.exists(os.path.join(self.ring, "lock", "big")))
        self.assertEqual(list(self.agent.ListLocks(None).locks), [])


class SupervisorTest(unittest.TestCase):
    """A scripted recorder: prints a status line per second; `crash` exits with 7 after LIVE seconds."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sup-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        self.script = os.path.join(self.dir, "rec.sh")
        with open(self.script, "w") as f:
            f.write("#!/bin/sh\ntrap 'echo {\\\"ev\\\":\\\"status\\\",\\\"state\\\":\\\"idle\\\"}; exit 0' TERM\n"
                    "echo '{\"ev\":\"segment_closed\",\"segno\":5,\"partial\":true}'\n"
                    "i=0; while :; do echo \"{\\\"ev\\\":\\\"status\\\",\\\"state\\\":\\\"running\\\",\\\"fps_in\\\":30,\\\"k\\\":$i,\\\"open_segno\\\":6}\"; i=$((i+1)); "
                    "[ -n \"$LIVE\" ] && [ $i -ge $LIVE ] && exit 7; sleep 1; done\n")
        os.chmod(self.script, stat.S_IRWXU)
        self.events = []
        self.rec = supervisor.Recorder(os.path.join(self.dir, "logs"), lambda s: ([self.script], dict(os.environ, LIVE=os.environ.get("LIVE", ""))), self.events.append, lambda m: None)
        supervisor.RESPAWN_DELAY_S = 0.3

    def tearDown(self):
        self.rec.stop()
        shutil.rmtree(self.dir)
        os.environ.pop("LIVE", None)

    def session(self):
        return {"id": "boot-1", "t_mono_start": 0, "start": {}}

    def test_start_stop_is_not_a_crash(self):
        self.rec.start(self.session())
        self.assertTrue(wait_for(lambda: self.rec.status.get("k", -1) >= 0))
        self.assertEqual(self.rec.state, supervisor.RUNNING)
        self.assertTrue(any(e.get("ev") == "segment_closed" for e in self.events))
        self.rec.stop()
        self.assertEqual(self.rec.state, supervisor.IDLE)
        self.rec.stop()                                           # idempotent
        time.sleep(0.6)
        self.assertIsNone(self.rec.proc)                          # no respawn after an expected exit
        with open(os.path.join(self.dir, "logs", "boot-1.jsonl")) as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual(lines[0]["ev"], "session")
        self.assertTrue(any(l.get("ev") == "recorder_exit" and l.get("expected") for l in lines))

    def test_crash_respawns_with_the_same_session(self):
        supervisor.MIN_LIFE_S = 0.5
        os.environ["LIVE"] = "2"
        self.rec.start(self.session())
        self.assertTrue(wait_for(lambda: self.rec.proc is None or self.rec.down, timeout=5))
        self.assertTrue(wait_for(lambda: self.rec.proc is not None and not self.rec.down, timeout=5))
        self.assertEqual(self.rec.state, supervisor.RUNNING)
        self.assertEqual(self.rec.session["id"], "boot-1")
        with open(os.path.join(self.dir, "logs", "boot-1.jsonl")) as f:
            kinds = [json.loads(l).get("ev") for l in f]
        self.assertIn("recorder_restart", kinds)
        self.assertEqual(kinds.count("session"), 1)

    def test_quick_death_ends_in_error_and_stop_clears_it(self):
        supervisor.MIN_LIFE_S = 10
        os.environ["LIVE"] = "1"
        self.rec.start(self.session())
        self.assertTrue(wait_for(lambda: self.rec.state == supervisor.ERROR, timeout=5))
        self.rec.stop()
        self.assertEqual(self.rec.state, supervisor.IDLE)

    def test_concurrent_stops(self):
        self.rec.start(self.session())
        self.assertTrue(wait_for(lambda: self.rec.status))
        ts = [threading.Thread(target=self.rec.stop) for _ in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(self.rec.state, supervisor.IDLE)


class ConfigTest(unittest.TestCase):
    """NodeConfig.effective: the session overrides, and the two ways they can contradict the node."""

    def cfg(self, **kw):
        from tileagent.agent import NodeConfig
        return NodeConfig(args("/dev/shm", **kw))

    def test_the_proxy_never_runs_faster_than_the_capture(self):
        # kpipe refuses --proxy-fps above --fps, so a session that lowers fps must lower the proxy too;
        # otherwise the recorder exits at once and the session ends in ERROR.
        c = self.cfg(proxy=True)
        c.proxy.enabled = True
        self.assertEqual(c.effective().proxy.fps, 15)
        self.assertEqual(c.effective(pb.StartRecordingRequest(fps=10)).proxy.fps, 10)
        self.assertEqual(c.effective(pb.StartRecordingRequest(fps=60)).proxy.fps, 15)

    def test_exposure_lines_is_refused_only_when_the_trigger_is_armed(self):
        c = self.cfg()
        armed = type("T", (), {"read": staticmethod(lambda: pb.TriggerSettings(armed=True))})()
        idle = type("T", (), {"read": staticmethod(lambda: pb.TriggerSettings(armed=False))})()
        req = pb.StartRecordingRequest(exposure_lines=800)
        self.assertEqual(c.effective(req, idle).sensor.exposure_lines, 800)
        self.assertEqual(c.effective(req).sensor.exposure_lines, 800)      # no trigger port: nothing to refuse
        with self.assertRaises(ApiError) as e:
            c.effective(req, armed)
        self.assertEqual(e.exception.status, 400)


class FanoutTest(unittest.TestCase):
    """LiveFanout against a socket that speaks the proxy branch's framed Annex-B, no recorder involved."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="live-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        self.sock_path = os.path.join(self.dir, "live.sock")
        self.commands = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(1)
        self.srv.settimeout(5)
        self.stop = threading.Event()
        self.fan = live.LiveFanout(self.sock_path, self.commands.append, say=lambda m: None)
        self.fan.set_ready(True)      # the recorder's `proxy ready` event; there is no recorder here
        self.writer = threading.Thread(target=self._serve, daemon=True)
        self.writer.start()

    def tearDown(self):
        self.stop.set()
        self.fan.shutdown()
        self.srv.close()
        self.writer.join(3)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _au(self, k, key):
        """One access unit as the recorder frames it: KAU1 header, then SPS/PPS on a keyframe, our SEI,
        then a slice."""
        rec = seitwin.build_sei(seitwin.pack_record(1, k, k * 1000, k & 0xFFFF, 0), "h264")
        head = b"\x00\x00\x00\x01\x67\xaa\x00\x00\x00\x01\x68\xbb" if key else b""
        slice_ = b"\x00\x00\x00\x01" + (b"\x65" if key else b"\x41") + b"\x88" + bytes([k & 0x7F]) * 200
        au = head + rec + slice_
        return live.HEADER.pack(live.MAGIC, len(au), 1 if key else 0) + au

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        k = 0
        with conn:
            while not self.stop.is_set():
                try:
                    conn.sendall(self._au(k, k % 15 == 0))
                except OSError:
                    return
                k += 1
                time.sleep(0.02)

    def _drain(self, client, want_aus, timeout=5):
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = client.read(0.2)
            if d is None:
                break
            buf += d
            if len(seitwin.split(buf, "h264")) > want_aus:
                break
        return buf

    def test_first_client_opens_the_valve_and_starts_at_a_keyframe(self):
        c = self.fan.subscribe()
        self.assertEqual(self.commands, ["proxy on"])
        buf = self._drain(c, 3)
        aus = seitwin.split(buf, "h264")
        self.assertTrue(aus)
        self.assertTrue(any(seitwin.is_idr(n, "h264") for n in aus[0][2]), "the stream must open on an IDR")
        recs = [seitwin.record_of(n, "h264") for _, _, n in aus[:-1]]
        self.assertTrue(all(recs), "every access unit carries the camsync SEI")
        self.assertEqual([r[1] for r in recs], list(range(recs[0][1], recs[0][1] + len(recs))))
        self.fan.unsubscribe(c)
        self.assertEqual(self.commands, ["proxy on", "proxy off"])

    def test_the_valve_closes_only_after_the_last_client(self):
        a, b = self.fan.subscribe(), self.fan.subscribe()
        self.assertEqual(self.commands, ["proxy on"])       # the second client does not re-open it
        self.assertEqual(self.fan.count(), 2)
        self._drain(a, 2)
        self.fan.unsubscribe(a)
        self.assertEqual(self.commands, ["proxy on"])
        self.fan.unsubscribe(b)
        self.assertEqual(self.commands, ["proxy on", "proxy off"])
        self.assertEqual(self.fan.count(), 0)

    def test_a_viewer_handing_over_to_another_keeps_the_valve_open(self):
        """The old client leaving and a new one arriving must not end with the valve shut and a dead
        stream: a Foxglove reconnect is exactly this sequence."""
        a = self.fan.subscribe()
        self._drain(a, 2)
        for _ in range(5):
            b = self.fan.subscribe()
            self.fan.unsubscribe(a)
            a = b
            self.assertTrue(self._drain(a, 1), "the new viewer got nothing after the old one left")
        self.fan.unsubscribe(a)
        self.assertEqual(self.commands[0], "proxy on")
        self.assertEqual(self.commands[-1], "proxy off")
        self.assertEqual(self.fan.count(), 0)

    def test_a_client_that_stops_reading_is_dropped(self):
        live.BACKLOG_CAP = 4096
        try:
            c = self.fan.subscribe()
            self.assertTrue(wait_for(lambda: self.fan.count() == 0, timeout=10), "a silent client must be dropped")
            self.assertTrue(c.dropped)
            self.assertEqual(self.commands, ["proxy on", "proxy off"])
        finally:
            live.BACKLOG_CAP = 2 << 20


if __name__ == "__main__":
    unittest.main()
