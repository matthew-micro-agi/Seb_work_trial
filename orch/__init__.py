"""orch: the tile.v1 client on the laptop (PLAN_ORCH.md). Two halves, control/ and data/, that never import
each other; both stand on client.py (tile.v1 over HTTP, the node list) and clock.py (wall <-> PTP).

    python3 -m orch <command> ...        # from the repo root; `python3 -m orch -h` lists the commands
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in (("icd", "py"), ("sei", "python")):
    p = os.path.join(ROOT, *sub)
    if p not in sys.path:
        sys.path.insert(0, p)
import tilev1 as pb  # noqa: E402,F401
