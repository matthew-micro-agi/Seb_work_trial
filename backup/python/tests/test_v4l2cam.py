import unittest

from camsync.v4l2cam import fourcc, to_nv12


class ConversionTest(unittest.TestCase):
    def test_fourcc(self):
        self.assertEqual(fourcc("YUYV"), 0x56595559)
        self.assertEqual(fourcc("Y10"), fourcc("Y10 "))

    def test_yuyv_keeps_luma_and_greys_chroma(self):
        yuyv = bytes([10, 128, 20, 128, 30, 128, 40, 128])       # 4x1 pixels
        nv12 = to_nv12(yuyv, 4, 1, "YUYV")
        self.assertEqual(nv12, bytes([10, 20, 30, 40]) + b"\x80\x80")

    def test_ten_bit_shifts_to_eight(self):
        raw = b"".join(v.to_bytes(2, "little") for v in (0, 4, 512, 1023))   # 4x1, 10-bit in 16-bit words
        self.assertEqual(to_nv12(raw, 4, 1, "Y10 "), bytes([0, 1, 128, 255]) + b"\x80\x80")

    def test_nv12_passthrough(self):
        self.assertEqual(to_nv12(b"\x01" * 6 + b"tail", 2, 2, "NV12"), b"\x01" * 6)


if __name__ == "__main__":
    unittest.main()
