"""Beacon unit tests: the trigger source's counting rules and log file, and the relay against a real
agent serving a real /v1/trigger/log over HTTP. The end-to-end (two recording sessions over one
uninterrupted pulse train) is tileagent/tests/beacon_pc.sh.

    TMPDIR=/dev/shm python3 -m unittest tileagent.tests.test_beacon      (from the repo root)
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from tileagent import beacon  # noqa: E402
from tileagent.agent import Agent, ApiError  # noqa: E402
from tileagent.http import serve  # noqa: E402
from tileagent.tests.test_agent import args  # noqa: E402

P = 33_333_333


def event(t_ns, index=0):
    """One struct ptp_extts_event as the kernel writes it."""
    return beacon._EXTTS_EVENT.pack(t_ns // 1_000_000_000, t_ns % 1_000_000_000, 0, index, 0, 0, 0)


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="beacon-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        self.path = os.path.join(self.dir, "trigger.log")
        self.src = beacon.TriggerSource(self.path, say=lambda m: None)
        self.src.epoch = 1

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_counts_received_pulses(self):
        self.src._consume(b"".join(event(i * P) for i in range(10)))
        self.assertEqual((self.src.epoch, self.src.k, self.src.pulses), (1, 9, 10))
        self.assertEqual(self.src.t_ptp, 9 * P)

    def test_a_missed_capture_does_not_advance_k(self):
        """K counts pulses received, not elapsed time: the beacon is what puts that right on the tile."""
        self.src._consume(b"".join(event(i * P) for i in range(10) if i != 4))
        self.assertEqual(self.src.k, 8)

    def test_a_one_second_gap_opens_an_epoch(self):
        self.src._consume(b"".join(event(i * P) for i in range(10)))
        self.src._consume(event(10 * P + 1_000_000_000))
        self.assertEqual((self.src.epoch, self.src.k), (2, 0))
        self.src._consume(event(10 * P + 1_000_000_000 + P))
        self.assertEqual((self.src.epoch, self.src.k), (2, 1))

    def test_events_of_another_channel_are_not_ours(self):
        self.src._consume(event(0) + event(P, index=3) + event(2 * P))
        self.assertEqual(self.src.k, 1)

    def test_the_log_line_and_the_seed_for_the_next_run(self):
        self.src._consume(b"".join(event(i * P) for i in range(3)))
        self.src._write_line()
        with open(self.path) as f:
            d = json.loads(f.read().strip())
        self.assertEqual((d["epoch"], d["k"], int(d["ptp_ns"])), (1, 2, 2 * P))
        self.assertIn("utc_ns", d)
        # A source that restarts cannot know what it missed, so it never reuses a number.
        self.assertEqual(beacon.TriggerSource(self.path)._seed_epoch(), 2)
        self.assertEqual(beacon.TriggerSource(self.path + ".missing")._seed_epoch(), 1)

    def test_the_log_stays_bounded(self):
        beacon.LOG_MAX_LINES, beacon.LOG_KEEP_LINES = 40, 20
        try:
            self.src._consume(b"".join(event(i * P) for i in range(60)))
            for k in range(60):
                self.src.k = k
                self.src._write_line()
            with open(self.path) as f:
                lines = f.read().splitlines()
            self.assertLessEqual(len(lines), 40)
            self.assertEqual(json.loads(lines[-1])["k"], 59)     # the tail is what survives
        finally:
            beacon.LOG_MAX_LINES, beacon.LOG_KEEP_LINES = 21_600, 10_800


class StubRecorder:
    def __init__(self, accept=True):
        self.accept, self.lines = accept, []

    def send(self, line):
        if not self.accept:
            return False
        self.lines.append(line)
        return True


class RelayTest(unittest.TestCase):
    """The relay against the real endpoint: one board is both source and consumer on the bench."""

    def setUp(self):
        self.ring = tempfile.mkdtemp(prefix="relay-", dir=os.environ.get("TMPDIR", "/dev/shm"))
        self.path = os.path.join(self.ring, "trigger.log")
        self.write(1, 100, 100 * P)
        self.agent = Agent(args(self.ring), say=lambda m: None)
        self.srv = serve(self.agent, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1/trigger/log" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.agent.shutdown()
        shutil.rmtree(self.ring)

    def write(self, epoch, k, t):
        with open(self.path, "a") as f:
            f.write(json.dumps({"epoch": epoch, "k": k, "ptp_ns": str(t), "utc_ns": None}) + "\n")

    def test_capability_is_declared_when_a_log_is_there(self):
        self.assertIn("trigger.log", self.agent.capabilities)

    def test_forwards_each_new_line_once(self):
        rec = StubRecorder()
        r = beacon.BeaconRelay(self.url, rec, say=lambda m: None)
        self.assertTrue(r.poll_once())
        self.assertEqual(rec.lines, ["beacon 1 100 %d" % (100 * P)])
        self.assertFalse(r.poll_once())                     # same pulse: nothing to say
        self.write(1, 130, 130 * P)
        self.assertTrue(r.poll_once())
        self.assertEqual(rec.lines[-1], "beacon 1 130 %d" % (130 * P))
        self.assertEqual((r.sent, r.errors), (2, 0))

    def test_only_the_tail_is_fetched(self):
        for k in range(200, 400):
            self.write(1, k, k * P)
        self.assertGreater(os.path.getsize(self.path), 4096)
        r = beacon.BeaconRelay(self.url, StubRecorder(), say=lambda m: None)
        self.assertEqual(beacon.parse_line(r.fetch())[1], 399)

    def test_a_refusing_recorder_is_retried_next_time(self):
        rec = StubRecorder(accept=False)
        r = beacon.BeaconRelay(self.url, rec, say=lambda m: None)
        self.assertFalse(r.poll_once())                     # nothing recording yet
        rec.accept = True
        self.assertTrue(r.poll_once())                      # the same line still goes out

    def test_a_source_that_is_not_there(self):
        r = beacon.BeaconRelay("http://127.0.0.1:1/v1/trigger/log", StubRecorder(), say=lambda m: None)
        self.assertFalse(r.poll_once())
        self.assertEqual(r.errors, 1)

    def test_404_before_the_first_pulse(self):
        os.unlink(self.path)
        with self.assertRaises(ApiError) as cm:
            self.agent.trigger_log_path()
        self.assertEqual(cm.exception.status, 404)


class ParseTest(unittest.TestCase):
    def test_parse_line(self):
        self.assertEqual(beacon.parse_line('{"epoch":2,"k":7,"ptp_ns":"99","utc_ns":null}'), (2, 7, 99))
        for bad in ("", "not json", "{}", '{"epoch":1,"k":2}', '{"epoch":1,"k":2,"ptp_ns":"x"}'):
            self.assertIsNone(beacon.parse_line(bad), bad)

    def test_the_event_struct_is_the_kernel_s(self):
        self.assertEqual(beacon._EXTTS_EVENT.size, 32)      # ptp_clock_time + index + flags + rsv[2]
        self.assertEqual(beacon._EXTTS_REQUEST.size, 16)
        self.assertEqual(struct.calcsize("q"), 8)


if __name__ == "__main__":
    unittest.main()
