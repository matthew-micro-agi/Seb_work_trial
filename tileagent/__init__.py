"""tile-agent: the tile's agent toward the orchestrator (DESIGN_RING_CONTROL.md §5–§8).

    python3 -m tileagent --ring /ring --port 8080 [--no-recorder] [--recorder CMD] [--budget 26G] ...

Modules: index (SQLite over the segments, one function indexes and rebuilds), locks (hard-link map),
supervisor (spawns and restarts the recorder, tees its stdout), trigger (ePWM adapter), agent (the
tile.v1 methods), http (the transport). Python, standard library plus the generated classes in icd/.
"""
