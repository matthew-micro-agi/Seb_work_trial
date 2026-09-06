import unittest

from camsync import sei

VPS, SPS, PPS = bytes([32 << 1, 1, 0xAA]), bytes([33 << 1, 1, 0xBB]), bytes([34 << 1, 1, 0xCC])
IDR = bytes([19 << 1, 1, 0x80, 0x11])          # first_slice_segment_in_pic_flag = 1
P_FIRST = bytes([1 << 1, 1, 0x80, 0x22])
P_MORE = bytes([1 << 1, 1, 0x00, 0x33])        # second slice of the same picture


class SeiTest(unittest.TestCase):
    def test_record_roundtrip_with_emulation_prevention(self):
        rec = sei.pack_record(1, 0, 0, 0, 0)     # zeros force 00 00 0x sequences in the payload
        nal = sei.build_sei(rec)[4:]
        self.assertNotIn(b"\x00\x00\x00", nal)
        self.assertNotIn(b"\x00\x00\x01", nal)
        self.assertEqual(sei.parse_sei(nal), (1, 0, 0, 0, 0))
        rec = sei.pack_record(7, 41200, 123456789012, 65535, 6)
        self.assertEqual(sei.parse_sei(sei.build_sei(rec)[4:]), (7, 41200, 123456789012, 65535, 6))

    def test_access_unit_split_and_sei_placement(self):
        stream = (b"\x00\x00\x00\x01" + VPS + b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS
                  + b"\x00\x00\x00\x01" + IDR + b"\x00\x00\x00\x01" + P_FIRST + b"\x00\x00\x01" + P_MORE
                  + b"\x00\x00\x00\x01" + P_FIRST)
        chunks = [stream[i:i + 5] for i in range(0, len(stream), 5)]     # arbitrary chunking
        aus = list(sei.access_units(chunks))
        self.assertEqual(aus, [[VPS, SPS, PPS, IDR], [P_FIRST, P_MORE], [P_FIRST]])
        s = sei.build_sei(sei.pack_record(1, 5, 9, 5, 0))
        out = sei.insert_sei(aus[0], s)
        self.assertEqual(out, b"\x00\x00\x00\x01" + VPS + b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS
                         + s + b"\x00\x00\x00\x01" + IDR)
        self.assertEqual(sei.record_of(next(sei.access_units([out])))[1], 5)


if __name__ == "__main__":
    unittest.main()
