"""The fault vocabulary has one source, kmatch/include/kmatch/types.hpp. The SEI carries the bits; the
proto's `Fault` enum value must be bit index + 1 with the same name.

    python3 sei/tests/test_faults.py
"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def kmatch_bits():
    src = open(os.path.join(ROOT, "kmatch/include/kmatch/types.hpp")).read()
    return {name: int(bit) for name, bit in re.findall(r"FAULT_(\w+) = 1u << (\d+)", src)}


def proto_values():
    src = open(os.path.join(ROOT, "icd/tile.proto")).read()
    body = re.search(r"enum Fault \{(.*?)\}", src, re.S).group(1)
    return {name: int(v) for name, v in re.findall(r"^\s*(\w+) = (\d+);", body, re.M) if name != "FAULT_NONE"}


class FaultVocabularyTest(unittest.TestCase):
    def test_bit_plus_one(self):
        bits, values = kmatch_bits(), proto_values()
        self.assertEqual(set(bits), set(values))
        for name, bit in bits.items():
            self.assertEqual(values[name], bit + 1, name)
        self.assertLessEqual(max(bits.values()), 15, "every bit must fit the 16-bit SEI flags field")


if __name__ == "__main__":
    unittest.main()
