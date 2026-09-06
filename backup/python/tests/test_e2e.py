"""Fake camera -> binder -> encoder -> file, with every fault injected, then the file is checked.

The binder picks the Wave5 encoder on the EVM and x265 on a PC.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

FPS = 60
FAULTS = "skip@50,drop@100,dup@150,late@200:12,skip@240:3"   # late 12 ms > half of the 16.7 ms period


class EndToEnd(unittest.TestCase):
    def test_faults_end_to_end(self):
        d = tempfile.mkdtemp()
        out, log = os.path.join(d, "rec.h265"), os.path.join(d, "rec.jsonl")
        cam = subprocess.Popen([sys.executable, "-m", "camsync.fakecam", "--width", "320", "--height", "240",
                                "--fps", str(FPS), "--frames", "300", "--counter-start", "65500", "--faults", FAULTS],
                               stdout=subprocess.PIPE)
        binder = subprocess.run([sys.executable, "-m", "camsync.binder", "--out", out, "--log", log, "--fps", str(FPS),
                                 "--check-s", "0.5"], stdin=cam.stdout, stderr=subprocess.PIPE)
        cam.stdout.close()                                       # before wait: a dead binder must not deadlock us
        cam.wait()
        self.assertEqual(binder.returncode, 0, binder.stderr.decode())

        with open(log) as f:
            events = [json.loads(line) for line in f]
        frames = [e for e in events if e["ev"] == "frame"]
        gaps = [e for e in events if e["ev"] == "gap"]
        # 300 edges: 1 + 3 skipped, 1 dropped, 1 duplicated -> 296 delivered frames
        self.assertEqual(len(frames), 296)
        self.assertEqual([(g["from_k"], g["to_k"], g["delivery_lost"], g["sensor_missed"]) for g in gaps[:2]],
                         [(50, 50, 0, 1), (100, 100, 1, 0)])
        self.assertEqual(sum(1 for e in events if e["ev"] == "dup"), 1)
        self.assertEqual([e["by"] for e in events if e["ev"] == "k_corrected"], [-1])
        self.assertEqual(gaps[-1]["from_k"], 240)
        self.assertEqual(gaps[-1]["to_k"], 242)
        self.assertEqual(frames[-1]["k"], 299)                    # last edge index, K aligned again
        self.assertLess(frames[-1]["s"], frames[0]["s"])           # the 16-bit counter wrapped inside the run

        check = subprocess.run([sys.executable, "-m", "camsync.check_stream", out, log], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn("access units 296  with SEI 296  without SEI 0", check.stdout)


if __name__ == "__main__":
    unittest.main()
