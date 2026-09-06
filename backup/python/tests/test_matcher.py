import unittest

from camsync.matcher import FLAG_CORRECTED, FLAG_DUP, FLAG_GAP_BEFORE, Matcher

P = 33_333_333  # 30 fps


class Feed:
    """Drives a Matcher with a sensor whose counter and clock we control."""

    def __init__(self, counter_start=0, check_frames=None):
        self.events = []
        self.m = Matcher(P, check_ns=(check_frames or 1_000_000) * P, log=self.events.append)
        self.t = 10 * P
        self.s = counter_start

    def frame(self, periods=1, exposures=1, late_ns=0):
        """periods: edges since the last delivered frame; exposures: sensor counter steps."""
        self.t += periods * P
        self.s = (self.s + exposures) & 0xFFFF
        return self.m.frame(self.t + late_ns, self.s)

    def gaps(self):
        return [e for e in self.events if e["ev"] == "gap"]


class MatcherTest(unittest.TestCase):
    def test_normal_run(self):
        f = Feed()
        ks = [f.frame().k for _ in range(10)]
        self.assertEqual(ks, list(range(10)))
        self.assertEqual(f.gaps(), [])

    def test_delivery_loss(self):
        f = Feed()
        f.frame()
        r = f.frame(periods=3, exposures=3)     # sensor exposed 3, we got the last one
        self.assertEqual((r.k, r.delivery_lost, r.sensor_missed), (3, 2, 0))
        self.assertTrue(r.flags & FLAG_GAP_BEFORE)
        self.assertEqual(f.gaps()[0]["from_k"], 1)
        self.assertEqual(f.gaps()[0]["to_k"], 2)
        self.assertEqual(f.frame().k, 4)

    def test_sensor_miss(self):
        f = Feed()
        f.frame()
        r = f.frame(periods=2, exposures=1)     # one edge fired, sensor did not expose
        self.assertEqual((r.k, r.delivery_lost, r.sensor_missed), (2, 0, 1))
        self.assertEqual(f.gaps()[0]["sensor_missed"], 1)
        self.assertEqual(f.frame().k, 3)        # M stays folded into K

    def test_both_kinds_in_one_gap(self):
        f = Feed()
        f.frame()
        r = f.frame(periods=4, exposures=2)     # 4 edges: 2 exposures (1 lost), 2 edges missed
        self.assertEqual((r.k, r.delivery_lost, r.sensor_missed), (4, 1, 2))

    def test_duplicate(self):
        f = Feed()
        f.frame()
        r = f.frame(periods=0, exposures=0)
        self.assertEqual((r.k, r.flags), (0, FLAG_DUP))
        self.assertEqual(f.frame().k, 1)
        self.assertEqual(f.gaps(), [])

    def test_counter_wrap(self):
        f = Feed(counter_start=65533)
        ks = [f.frame().k for _ in range(6)]    # 65534, 65535, 0, 1, 2, 3
        self.assertEqual(ks, list(range(6)))
        self.assertEqual(f.gaps(), [])

    def test_late_frame_is_corrected_by_window_check(self):
        f = Feed(check_frames=10)
        for _ in range(5):
            f.frame()
        r = f.frame(late_ns=int(0.6 * P))        # delivered 0.6 periods late -> misread as a sensor miss
        self.assertEqual(r.k, 6)                 # off by one from here on
        ks = []
        for _ in range(10):
            ks.append(f.frame())
        corrected = [r for r in ks if r.flags & FLAG_CORRECTED]
        self.assertEqual(len(corrected), 1)
        self.assertEqual(ks[-1].k, 15)           # 16 frames delivered -> last K is 15 again
        self.assertEqual([e["by"] for e in f.events if e["ev"] == "k_corrected"], [-1])

    def test_window_check_is_silent_when_correct(self):
        f = Feed(check_frames=10)
        for _ in range(40):
            f.frame()
        self.assertFalse([e for e in f.events if e["ev"] == "k_corrected"])

    def test_trigger_stall_alarm(self):
        f = Feed()
        f.m.edge(f.t)
        f.frame()
        f.m.tick(f.t + 3 * P)
        self.assertEqual([e["ev"] for e in f.events if e["ev"].startswith("trigger")], ["trigger_stall"])
        f.frame(periods=90, exposures=1)         # frames resume 3 s later
        self.assertTrue(f.gaps()[0]["trigger_stalled"])
        f.m.edge(f.t)
        self.assertEqual([e["ev"] for e in f.events if e["ev"].startswith("trigger")],
                         ["trigger_stall", "trigger_resumed"])

    def test_rearm_starts_new_epoch(self):
        f = Feed()
        for _ in range(3):
            f.frame()
        f.m.rearm()
        r = f.frame(periods=30, exposures=7)
        self.assertEqual((f.m.epoch, r.k, r.flags), (2, 0, 0))

    def test_strobe_mismatch_is_reported(self):
        f = Feed(check_frames=10)
        f.frame()
        for _ in range(12):
            f.frame()
            f.m.strobe(f.t)
            f.m.strobe(f.t)                      # two strobes per exposure
        self.assertTrue([e for e in f.events if e["ev"] == "strobe_mismatch"])


if __name__ == "__main__":
    unittest.main()
