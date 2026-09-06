"""The index: one SQLite row per segment, every column read from the file (DESIGN_RING_CONTROL.md §5).
index_segment() is the one function for the live doorbell and the rebuild; Index is the one writer."""
import os
import sqlite3
import threading

from .pb import UNMATCHED_K, sei

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS segments (
  segno         INTEGER PRIMARY KEY,
  session       TEXT NOT NULL,
  epoch_first   INTEGER, k_first INTEGER,
  epoch_last    INTEGER, k_last  INTEGER,
  t_mono_first  INTEGER NOT NULL, t_mono_last INTEGER NOT NULL,
  ptp_minus_mono INTEGER,
  frames        INTEGER NOT NULL,
  bytes         INTEGER NOT NULL,
  faults        INTEGER NOT NULL,
  partial       INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS segments_t ON segments(t_mono_last);
CREATE INDEX IF NOT EXISTS segments_k ON segments(epoch_last, k_last);
"""
SCHEMA_VERSION = "1"
COLUMNS = ("segno", "session", "epoch_first", "k_first", "epoch_last", "k_last", "t_mono_first", "t_mono_last",
           "ptp_minus_mono", "frames", "bytes", "faults", "partial")


def index_segment(path, segno, session, offset_at, partial=False):
    """Walk the AUs of one segment file; return the row as a dict. offset_at(t_mono) -> ptp_minus_mono or None."""
    with open(path, "rb") as f:
        data = f.read()
    aus = sei.split(data)
    records = [r for r in (sei.record_of(nals) for _, _, nals in aus) if r]
    if not records:
        raise ValueError("%s: no access unit with a camsync SEI" % path)
    matched = [r for r in records if r[1] != UNMATCHED_K]
    faults = 0
    for r in records:
        faults |= r[4]
    return {
        "segno": segno, "session": session,
        "epoch_first": matched[0][0] if matched else None, "k_first": matched[0][1] if matched else None,
        "epoch_last": matched[-1][0] if matched else None, "k_last": matched[-1][1] if matched else None,
        "t_mono_first": records[0][2], "t_mono_last": records[-1][2],
        "ptp_minus_mono": offset_at(records[-1][2]),
        "frames": len(aus), "bytes": os.path.getsize(path), "faults": faults, "partial": 1 if partial else 0,
    }


class Index:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    def upsert(self, row):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO segments (%s) VALUES (%s)" % (",".join(COLUMNS), ",".join("?" * len(COLUMNS))),
                            tuple(row[c] for c in COLUMNS))

    def delete(self, segno):
        with self.lock:
            self.db.execute("DELETE FROM segments WHERE segno = ?", (segno,))

    def get(self, segno):
        with self.lock:
            return self.db.execute("SELECT * FROM segments WHERE segno = ?", (segno,)).fetchone()

    def rows(self, segnos):
        with self.lock:
            return [r for s in segnos if (r := self.get(s)) is not None]

    def all_segnos(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT segno FROM segments")}

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    def session_range(self, session):
        with self.lock:
            r = self.db.execute("SELECT MIN(segno), MAX(segno) FROM segments WHERE session = ?", (session,)).fetchone()
            return (r[0] or 0, r[1] or 0)

    def sessions(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT DISTINCT session FROM segments")}

    def bytes_of(self, segnos):
        with self.lock:
            return sum(r["bytes"] for r in self.rows(segnos))

    def select(self, sel, default_offset):
        """Rows matching a tile.v1 Selector, ascending segno. default_offset stands in for a NULL ptp_minus_mono."""
        kind = sel.WhichOneof("by")
        # Frame times are CLOCK_MONOTONIC, which restarts at every boot, so rows written under an earlier
        # boot are not comparable with the current ones. The session id starts with the boot id, and segno
        # is globally monotonic, so the newest segment names the current boot. Both time selectors are
        # confined to it; segno and K selectors need no such filter.
        BOOT = "substr(session, 1, instr(session, '-') - 1)"
        CUR = "(SELECT %s FROM segments ORDER BY segno DESC LIMIT 1)" % BOOT
        if kind == "last_s":
            q, args = ("SELECT * FROM segments WHERE {b} = {c} "
                       "AND t_mono_last >= (SELECT MAX(t_mono_last) FROM segments WHERE {b} = {c}) - ? "
                       "ORDER BY segno".format(b=BOOT, c=CUR),
                       (int(sel.last_s * 1e9),))
        elif kind == "ptp":
            q, args = ("SELECT * FROM segments WHERE {b} = {c} "
                       "AND t_mono_last + COALESCE(ptp_minus_mono, ?) >= ? "
                       "AND t_mono_first + COALESCE(ptp_minus_mono, ?) <= ? ORDER BY segno".format(b=BOOT, c=CUR),
                       (default_offset, sel.ptp.since_ns, default_offset, sel.ptp.until_ns))
        elif kind == "k":
            q, args = ("SELECT * FROM segments WHERE (epoch_last, k_last) >= (?, ?) AND (epoch_first, k_first) <= (?, ?) ORDER BY segno",
                       (sel.k.epoch, sel.k.k_from, sel.k.epoch, sel.k.k_to))
        elif kind == "segno":
            q, args = ("SELECT * FROM segments WHERE segno BETWEEN ? AND ? ORDER BY segno", (sel.segno.__getattribute__("from"), sel.segno.to))
        else:
            raise ValueError("selector: exactly one of last_s, ptp, k, segno must be set")
        with self.lock:
            return self.db.execute(q, args).fetchall()
