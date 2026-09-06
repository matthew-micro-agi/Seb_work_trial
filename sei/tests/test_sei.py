"""The Python twin against sei/tests/vectors.txt, plus the splitter's offsets.

    python3 sei/tests/test_sei.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
import sei  # noqa: E402


def vectors():
    with open(os.path.join(HERE, "vectors.txt")) as f:
        return [line.split() for line in f if line.strip() and not line.startswith("#")]


class SeiTwinTest(unittest.TestCase):
    def test_vectors(self):
        stream, aus, codec, codecs = None, [], "h265", []
        for v in vectors():
            if v[0] == "C":
                if stream is not None:
                    self.check_split(stream, aus, codec)
                    stream, aus = None, []
                codec = v[1]
                codecs.append(codec)
            elif v[0] == "R":
                rec = tuple(int(x) for x in v[1:6])
                nal = sei.build_sei(sei.pack_record(*rec), codec)
                self.assertEqual(nal.hex(), v[6])
                self.assertEqual(sei.parse_sei(nal[4:], codec), rec)
                self.assertNotIn(b"\x00\x00\x00", nal[4:])
                self.assertNotIn(b"\x00\x00\x01", nal[4:])
            elif v[0] == "S":
                if stream is not None:
                    self.check_split(stream, aus, codec)
                stream, aus = bytes.fromhex(v[1]), []
            elif v[0] == "A":
                aus.append([bytes.fromhex(n) for n in v[1:]])
            elif v[0] == "I":
                au = list(sei.access_units([bytes.fromhex(v[7])], codec))[int(v[1])]
                rec = tuple(int(x) for x in v[2:7])
                out = sei.insert_sei(au, sei.build_sei(sei.pack_record(*rec), codec), codec)
                self.assertEqual(out.hex(), v[8])
                self.assertEqual(sei.record_of(next(sei.access_units([out], codec)), codec), rec)
        self.check_split(stream, aus, codec)
        self.assertEqual(codecs, ["h265", "h264"])

    def test_codecs_do_not_cross(self):
        """One codec's SEI is not the other's: a mis-set codec fails loudly, not silently."""
        rec = sei.pack_record(1, 2, 3, 4, 5)
        self.assertIsNone(sei.parse_sei(sei.build_sei(rec, "h265")[4:], "h264"))
        self.assertIsNone(sei.parse_sei(sei.build_sei(rec, "h264")[4:], "h265"))
        self.assertRaises(ValueError, sei.split, b"", "vp9")

    def check_split(self, stream, aus, codec):
        parts = sei.split(stream, codec)
        self.assertEqual([nals for _, _, nals in parts], aus)
        self.assertEqual(list(sei.access_units([stream[i:i + 7] for i in range(0, len(stream), 7)], codec)), aus)
        # the ranges tile the buffer and each range re-splits to exactly its own NALs
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], len(stream))
        for (b0, e0, _), (b1, _, _) in zip(parts, parts[1:]):
            self.assertEqual(e0, b1)
        for b, e, nals in parts:
            self.assertEqual(sei.split(stream[b:e], codec)[0][2], nals)
        if len(parts) >= 2:
            self.assertEqual(sei.last_complete_au_end(stream, codec), parts[-2][1])
        self.assertEqual(sei.last_complete_au_end(stream[:parts[0][1]], codec), 0)


if __name__ == "__main__":
    unittest.main()
