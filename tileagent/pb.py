"""Paths into the tree: the generated tile.v1 classes (icd/py) and the Python sei twin (sei/python)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "icd", "py"))
sys.path.insert(0, os.path.join(ROOT, "sei", "python"))
import tilev1 as pb  # noqa: E402,F401
import sei  # noqa: E402,F401

UNMATCHED_K = 0xFFFFFFFF          # kpipe stores K = -1 as uint32 in the SEI


def fault_values(bits):
    """kmatch fault bits -> proto Fault values (bit index + 1), unknown bits dropped."""
    known = set(pb.Fault.values())
    return [i + 1 for i in range(16) if bits & (1 << i) and (i + 1) in known]
