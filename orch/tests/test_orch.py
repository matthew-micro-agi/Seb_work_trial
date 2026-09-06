"""orch tests against a tile-agent this file starts on a copy of the fixture ring (segfile/tests/fixture.sh).

    RING_FIXTURE=/dev/shm/ring-fixture python3 -m unittest orch.tests.test_orch          (from the repo root)

The agent gets --lock-budget 2M so a fetch of the whole ring goes through lock windows. The bridge and
mcap tests need the foxglove SDK (a venv, TESTING.md) and are skipped without it; the live-video test
also needs pipeline/build/kpipe, which it runs on the synthetic source.
"""
import contextlib
import datetime
import filecmp
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from orch import clock, pb  # noqa: E402
from orch.__main__ import main  # noqa: E402
from orch.client import Client, TileError  # noqa: E402

FIXTURE = os.environ.get("RING_FIXTURE")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(list(argv))
    return rc, out.getvalue()


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(FIXTURE, "RING_FIXTURE not set")
class AgentCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="orch-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        cls.ring = os.path.join(cls.tmp, "ring")
        shutil.copytree(os.path.join(FIXTURE, "seg"), os.path.join(cls.ring, "seg"))
        cls.port = free_port()
        cls.host = "127.0.0.1:%d" % cls.port
        cls.agent = subprocess.Popen([sys.executable, "-m", "tileagent", "--ring", cls.ring, "--port", str(cls.port), "--no-recorder",
                                      "--lock-budget", "2M", "--node-id", "aa:bb:cc:dd:ee:ff"], cwd=ROOT, stderr=subprocess.DEVNULL)
        cls.c = Client(cls.host)
        for _ in range(100):
            try:
                if pb.INDEX_REBUILDING not in cls.c.rpc("GetHealth").faults_active:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        cls.rows = list(cls.c.rpc("ListSegments", pb.ListSegmentsRequest(select=pb.Selector(last_s=1e9))).segments)
        cls.cal = clock.calibrate(cls.c.time_samples())

    @classmethod
    def tearDownClass(cls):
        cls.agent.terminate()
        cls.agent.wait(10)
        shutil.rmtree(cls.tmp)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def window(self, a, b):
        """--from/--to arguments that cover segments a..b exactly (half a second inside their edges)."""
        rows = {s.segno: s for s in self.rows}
        w0, w1 = self.cal.to_wall(rows[a].t_ptp_first_ns) + 500_000_000, self.cal.to_wall(rows[b].t_ptp_last_ns) - 500_000_000
        f = lambda ns: datetime.datetime.fromtimestamp(ns / 1e9).astimezone()  # noqa: E731
        return ["--from", f(w0).strftime("%H:%M:%S"), "--to", f(w1).strftime("%H:%M:%S"), "--date", f(w0).strftime("%Y-%m-%d")]

    def assert_identical(self, out):
        seg = os.path.join(out, "aabbccddeeff", "seg")
        names = [n for n in os.listdir(seg) if n.endswith(".h265")]
        self.assertTrue(names)
        for n in names:
            self.assertTrue(filecmp.cmp(os.path.join(seg, n), os.path.join(self.ring, "seg", n), shallow=False), n)
        self.assertFalse([n for n in os.listdir(seg) if n.endswith(".part")])
        return sorted(int(n[:12]) for n in names)


class ControlTest(AgentCase):
    def test_nodes_health_time(self):
        rc, out = run("--nodes", self.host, "nodes")
        self.assertEqual(rc, 0)
        self.assertIn("aa:bb:cc:dd:ee:ff hw=pc", out)
        self.assertIn("caps=core,segments,sessions,locks,metrics", out)
        rc, out = run("--nodes", self.host, "health")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"IDLE\s+fps=0\s+K=0/0 segs=19 used=\d+M locked=\dM faults=PTP_UNLOCKED")
        rc, out = run("--nodes", self.host, "time")
        self.assertIn("ptp_locked=False", out)
        self.assertIn("PTP free-running", out)

    def test_undeclared_and_unreachable_are_reported(self):
        rc, out = run("--nodes", self.host, "start")
        self.assertEqual(rc, 1)
        self.assertIn("501 NOT_IMPLEMENTED", out)
        rc, out = run("--nodes", "127.0.0.1:%d,%s" % (free_port(), self.host), "nodes")
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", out)
        self.assertIn("aa:bb:cc:dd:ee:ff", out)          # the other node still answered

    def test_lock_unlock_locks(self):
        rc, out = run("--nodes", self.host, "lock", "t-1", "--segno", "13:13")
        self.assertEqual(rc, 0); self.assertIn("created t-1: 1 segments", out)
        rc, out = run("--nodes", self.host, "lock", "t-1", "--segno", "14:14")
        self.assertIn("extended t-1: 2 segments", out); self.assertIn("added=[14]", out)
        rc, out = run("--nodes", self.host, "locks")
        self.assertIn("t-1", out)
        rc, out = run("--nodes", self.host, "unlock", "t-1")
        self.assertEqual(rc, 0)
        rc, out = run("--nodes", self.host, "unlock", "t-1")
        self.assertEqual(rc, 1); self.assertIn("404 NOT_FOUND", out)

    def test_segments_by_k_and_last(self):
        rc, out = run("--nodes", self.host, "segments", "--k", "2:0:30")
        self.assertIn("K 1:840..2:24", out); self.assertIn("EPOCH_CHANGE", out)
        rc, out = run("--nodes", self.host, "segments", "--last", "6")
        self.assertIn("4 segments", out)

    def test_selector_grammar(self):
        with self.assertRaises(SystemExit):
            run("--nodes", self.host, "segments", "--last", "6", "--k", "1:0:1")
        with self.assertRaises(SystemExit):
            run("--nodes", self.host, "segments", "--from", "08:00")


class ClockTest(unittest.TestCase):
    def test_calibrate_min_rtt_midpoint(self):
        t = pb.Time(ptp_ns=1_000, ptp_locked=False, boot_id="b")
        cal = clock.calibrate([(100, 900, t), (400, 600, t), (0, 2000, t)])
        self.assertEqual((cal.offset_ns, cal.rtt_ns), (1_000 - 500, 200))
        self.assertEqual(cal.to_wall(cal.to_ptp(12345)), 12345)
        self.assertIn("free-running", cal.warnings()[0])
        cal = clock.calibrate([(0, 2, pb.Time(ptp_ns=37 * clock.NS + 1, ptp_locked=True, boot_id="b"))])
        self.assertEqual(cal.warnings(), [])
        cal = clock.calibrate([(0, 2, pb.Time(ptp_ns=5 * clock.NS, ptp_locked=True, boot_id="b"))])
        self.assertIn("TAI", cal.warnings()[0])

    def test_edges_and_boot(self):
        self.assertIsNone(clock.edges(0, 10, 20, 30))
        self.assertIsNone(clock.edges(40, 50, 20, 30))
        self.assertIsNone(clock.edges(0, 10, 0, 0))
        self.assertEqual(clock.edges(10 * clock.NS, 40 * clock.NS, 20 * clock.NS, 30 * clock.NS), {"head_missing_s": 10.0, "tail_missing_s": 10.0})
        self.assertEqual(clock.edges(22, 28, 20, 30), {"head_missing_s": 0.0, "tail_missing_s": 0.0})
        self.assertTrue(clock.same_boot("ecdbe531-393525312", "ecdbe531-231d-42f6"))
        self.assertFalse(clock.same_boot("11111111-393525312", "ecdbe531-231d-42f6"))
        self.assertIsNone(clock.same_boot("recovered", "ecdbe531-231d-42f6"))

    def test_local_to_utc(self):
        ns = clock.local_to_utc_ns("08:00", "2026-09-06")
        self.assertEqual(datetime.datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S"), "2026-09-06 08:00:00")
        self.assertEqual(clock.local_to_utc_ns("08:00:30", "2026-09-06") - ns, 30 * clock.NS)
        with self.assertRaises(ValueError):
            clock.local_to_utc_ns("8am")


class FetchTest(AgentCase):
    def test_time_window_identical_and_manifest(self):
        out = self.out("w")
        rc, text = run("--nodes", self.host, "fetch", *self.window(16, 20), "--out", out, "--check")
        self.assertEqual(rc, 0, text)
        segnos = self.assert_identical(out)
        self.assertIn(segnos[0], (15, 16))                               # 15 overlaps by its last frame
        self.assertEqual(segnos[-1], 20)
        self.assertIn("check: %d files, 0 with findings" % len(segnos), text)
        self.assertIn("rebuilt index with no session", text)
        m = load_json(os.path.join(out, "aabbccddeeff", "manifest.json"))
        self.assertEqual(m["node"]["nodeId"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(m["time"]["boot_id"], self.cal.boot_id)
        self.assertEqual(len(m["segments"]["segments"]), len(segnos))
        self.assertIsNone(m["lock"])
        self.assertEqual(len(self.c.rpc("ListLocks").locks), 0)

    def test_resume_skips_complete_continues_part(self):
        out = self.out("r")
        rc, text = run("--nodes", self.host, "fetch", "--segno", "16:19", "--out", out)
        self.assertEqual(rc, 0, text)
        seg = os.path.join(out, "aabbccddeeff", "seg")
        full = read(os.path.join(seg, "000000000017.h265"))
        with open(os.path.join(seg, "000000000017.h265.part"), "wb") as f:
            f.write(full[:200_000])                                       # half a file: 206 continues it
        os.unlink(os.path.join(seg, "000000000017.h265"))
        os.unlink(os.path.join(seg, "000000000018.h265"))                 # gone: fetched again
        with open(os.path.join(seg, "000000000019.h265.part"), "wb") as f:
            f.write(full * 2)                                             # longer than the file: 416, refetched
        os.unlink(os.path.join(seg, "000000000019.h265"))
        rc, text = run("--nodes", self.host, "fetch", "--segno", "16:19", "--out", out)
        self.assertEqual(rc, 0, text)
        self.assertIn("3 fetched (1 resumed), 1 already complete, 0 vanished", text)
        self.assertEqual(self.assert_identical(out), [16, 17, 18, 19])

    def test_vanished_is_reported(self):
        from orch.data import fetch as F
        import argparse
        victim = os.path.join(self.ring, "seg", "000000000030.h265")
        keep = read(victim)

        def remove(node):
            os.unlink(victim)
        try:
            for lock in (False, True):          # runs last (alphabetical): PutLock drops the vanished row from the index
                args = argparse.Namespace(nodes=self.host, config=None, out=self.out("v%d" % lock), no_lock=not lock, keep_lock=False, check=False, jobs=4,
                                          requester="test", last=None, since=None, until=None, date=None, k=None, segno="29:31")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = F.main(args, between_list_and_copy=remove)
                self.assertEqual(rc, 1, buf.getvalue())
                self.assertIn("vanished", buf.getvalue())
                self.assertIn("1 vanished", buf.getvalue())
                self.assertEqual(sorted(os.listdir(os.path.join(self.out("v%d" % lock), "aabbccddeeff", "seg"))), ["000000000029.h265", "000000000031.h265"])
                with open(victim, "wb") as f:
                    f.write(keep)
        finally:
            with open(victim, "wb") as f:
                f.write(keep)

    def test_k_route_matches_time_route(self):
        rows = {s.segno: s for s in self.rows}
        rc, _ = run("--nodes", self.host, "fetch", "--k", "%d:%d:%d" % (rows[16].first.epoch, rows[16].first.k, rows[20].last.k), "--out", self.out("k"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.assert_identical(self.out("k")), [16, 17, 18, 19, 20])

    def test_lock_windows_under_the_cap(self):
        rc, text = run("--nodes", self.host, "fetch", "--last", "1e9", "--out", self.out("all"))
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"lock window: [34] of 19 remaining")     # 2 MB cap, 0.5 MB segments
        self.assertEqual(len(self.assert_identical(self.out("all"))), 19)
        self.assertEqual(len(self.c.rpc("ListLocks").locks), 0)
        rc, text = run("--nodes", self.host, "fetch", "--last", "1e9", "--out", self.out("all"))
        self.assertIn("0 fetched (0 resumed), 19 already complete", text)

    def test_refused_outside_and_truncation_printed(self):
        rc, text = run("--nodes", self.host, "fetch", "--from", "00:00", "--to", "00:01", "--date", "2000-01-01", "--out", self.out("x"))
        self.assertEqual(rc, 1)
        self.assertIn("refused: requested 2000-01-01 00:00:00", text)
        self.assertIn("footage on this tile spans", text)
        w = self.window(13, 14)
        w[1] = (datetime.datetime.strptime(w[1], "%H:%M:%S") - datetime.timedelta(seconds=20)).strftime("%H:%M:%S")
        rc, text = run("--nodes", self.host, "fetch", *w, "--out", self.out("t"))
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"head missing (19|20|21) s, tail missing 0 s")

    def test_boot_change_refuses_resume(self):
        out = self.out("b")
        run("--nodes", self.host, "fetch", "--segno", "13:13", "--out", out)
        p = os.path.join(out, "aabbccddeeff", "manifest.json")
        m = load_json(p)
        m["time"]["boot_id"] = "deadbeef-0"
        with open(p, "w") as f:
            json.dump(m, f)
        rc, text = run("--nodes", self.host, "fetch", "--segno", "13:13", "--out", out)
        self.assertEqual(rc, 1); self.assertIn("rebooted", text)


def have_foxglove():
    try:
        import foxglove, websockets  # noqa: F401
        return True
    except ImportError:
        return False


def bridge_probe(port, timeout=20):
    """Subscribe to everything the bridge advertises and collect one message per topic, as Foxglove does."""
    import asyncio
    import struct
    import websockets

    async def probe():
        async with websockets.connect("ws://127.0.0.1:%d" % port, subprotocols=["foxglove.sdk.v1"], max_size=None) as ws:
            channels = {}
            while not channels:
                m = await asyncio.wait_for(ws.recv(), timeout)
                d = json.loads(m) if isinstance(m, str) else None
                if d and d.get("op") == "advertise":
                    channels = {c["id"]: c for c in d["channels"]}
            ids = list(channels)
            await ws.send(json.dumps({"op": "subscribe", "subscriptions": [{"id": i, "channelId": c} for i, c in enumerate(ids)]}))
            seen = {}
            while len(seen) < len(channels):
                m = await asyncio.wait_for(ws.recv(), timeout)
                if isinstance(m, str):
                    continue
                op, sub, _ = struct.unpack_from("<BIQ", m, 0)
                if op == 1:
                    ch = channels[ids[sub]]
                    seen.setdefault(ch["topic"], (ch["schemaName"], m[13:]))
            return seen
    return asyncio.run(probe())


@unittest.skipUnless(have_foxglove(), "foxglove SDK and websockets not importable (use the venv, TESTING.md)")
class MonitoringTest(AgentCase):
    def test_bridge_publishes_every_channel(self):
        """The bridge as Foxglove sees it: serverInfo, advertise, subscribe, one decoded message per channel."""
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "orch", "--nodes", self.host, "bridge", "--port", str(port), "--replay-segment"],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for _ in range(100):
                line = proc.stdout.readline()
                if "ws://" in line:
                    break
            self.assertIn("ws://127.0.0.1:%d" % port, line)
            seen = bridge_probe(port)
        finally:
            proc.terminate()
            proc.wait(10)
        base = "/tile/aabbccddeeff/"
        self.assertEqual(sorted(seen), [base + n for n in ("frame", "health", "link", "telemetry", "video")])
        self.assertEqual(seen[base + "health"][0], "tile.v1.Health")
        h = pb.Health.FromString(seen[base + "health"][1])
        self.assertEqual(h.ring.segments, 19)
        self.assertEqual(pb.Telemetry.FromString(seen[base + "telemetry"][1]).fps_in, 0)
        self.assertEqual(json.loads(seen[base + "link"][1]), {"reachable": True, "age_s": 0.0, "error": ""})
        fr = json.loads(seen[base + "frame"][1])
        self.assertEqual(seen[base + "frame"][0], "orch.Frame")
        self.assertEqual(fr["epoch"], 2)
        self.assertTrue(fr["matched"])
        self.assertEqual(seen[base + "video"][0], "foxglove.CompressedVideo")

@unittest.skipUnless(have_foxglove(), "foxglove SDK and websockets not importable (use the venv, TESTING.md)")
@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "pipeline", "build", "kpipe")), "pipeline/build/kpipe not built")
class LiveBridgeTest(unittest.TestCase):
    """The whole live path on this PC: kpipe's proxy branch -> tile-agent /v1/live -> orch bridge ->
    a Foxglove client. The tile is a real tile-agent driving a real kpipe on the synthetic source."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="orch-live-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        cls.port = free_port()
        cls.host = "127.0.0.1:%d" % cls.port
        cls.agent = subprocess.Popen(
            [sys.executable, "-m", "tileagent", "--ring", os.path.join(cls.tmp, "ring"), "--port", str(cls.port),
             "--budget", "100M", "--bitrate", "2000000", "--segment-s", "2", "--proxy", "--node-id", "aa:bb:cc:dd:ee:01",
             "--recorder", "pipeline/build/kpipe --fake --enc x265enc --proxy-enc x264enc"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.c = Client(cls.host)
        for _ in range(100):
            try:
                cls.c.rpc("GetNode")
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.agent.terminate()
        cls.agent.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_live_video_reaches_a_foxglove_client_as_h264_with_its_K(self):
        node = self.c.rpc("GetNode")
        self.assertIn("live", node.capabilities)
        self.c.rpc("StartRecording", pb.StartRecordingRequest(edges=pb.StartRecordingRequest.GRID, latency_ns=33_333_333))
        time.sleep(2)
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "orch", "--nodes", self.host, "bridge", "--port", str(port)],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for _ in range(100):
                line = proc.stdout.readline()
                if "ws://" in line:
                    break
            seen = bridge_probe(port)
            self.assertEqual(int(self.c.rpc("GetRecording").live_clients), 1, "the bridge is the tile's live client")
        finally:
            proc.terminate()
            proc.wait(10)
        base = "/tile/aabbccddee01/"
        self.assertEqual(sorted(seen), [base + n for n in ("frame", "health", "link", "telemetry", "video")])
        self.assertEqual(seen[base + "video"][0], "foxglove.CompressedVideo")
        self.assertIn(b"h264", seen[base + "video"][1], "CompressedVideo.format must say h264")
        fr = json.loads(seen[base + "frame"][1])
        self.assertTrue(fr["matched"] and fr["k"] > 0, "every live frame names its trigger index: %s" % fr)
        self.assertEqual(fr["faults"], [])
        self.c.rpc("StopRecording")


@unittest.skipUnless(have_foxglove(), "foxglove SDK and websockets not importable (use the venv, TESTING.md)")
class McapTest(AgentCase):
    def test_mcap_from_a_fetched_directory(self):
        from mcap.reader import make_reader
        out = self.out("m")
        rc, _ = run("--nodes", self.host, "fetch", "--segno", "16:17", "--out", out)
        self.assertEqual(rc, 0)
        rc, text = run("mcap", os.path.join(out, "aabbccddeeff"))
        self.assertEqual(rc, 0)
        self.assertIn("120 access units", text)
        path = os.path.join(out, "aabbccddeeff", "aabbccddeeff.mcap")
        with open(path, "rb") as f:
            s = make_reader(f).get_summary()
            topics = {c.topic: (c.message_encoding, s.schemas[c.schema_id].name) for c in s.channels.values()}
            self.assertEqual(topics, {"/tile/aabbccddeeff/video": ("protobuf", "foxglove.CompressedVideo"), "/tile/aabbccddeeff/frame": ("json", "orch.Frame")})
            self.assertEqual(s.statistics.message_count, 240)
            self.assertEqual(s.statistics.attachment_count, 1)      # manifest.json; the fixture ring has no session logs


if __name__ == "__main__":
    unittest.main()
