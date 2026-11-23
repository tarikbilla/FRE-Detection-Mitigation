"""Durable local outbox (SQLite, WAL, synchronous=FULL).

Every document is written here *before* any network I/O, so observations
survive network outages, Atlas maintenance, process crashes and power loss.
Rows are keyed by ``(collection, _id)``; inserting the same key twice is a
no-op (duplicate prevention on the producer side - MongoDB upserts provide it
on the server side). Rows are deleted only after MongoDB acknowledged them.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bson import json_util
from bson.json_util import JSONMode, JSONOptions

log = logging.getLogger(__name__)

JSON_OPTS = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True)
# When the buffer is full, drop the bulkiest / most reproducible data first.
# Telegrams (decoded signal) and sessions (metadata) are never dropped.
PRUNE_ORDER = ("spectra", "observations")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'insert',
    doc        TEXT NOT NULL,
    created_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS ix_outbox_created ON outbox(created_at);
CREATE TABLE IF NOT EXISTS dead_letter (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    doc        TEXT NOT NULL,
    error      TEXT,
    failed_at  REAL NOT NULL,
    PRIMARY KEY (collection, id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class BufferedRow:
    collection: str
    id: str
    mode: str
    doc: dict
    attempts: int


def dumps(doc: dict) -> str:
    return json_util.dumps(doc, json_options=JSON_OPTS)


def loads(text: str) -> dict:
    return json_util.loads(text, json_options=JSON_OPTS)


class LocalBuffer:
    def __init__(self, path: str | Path, max_rows: int = 200_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self.pruned_rows = 0
        # optional TimeArchive: every record put into the buffer is also filed locally by its timestamp
        self.archive = None

    # ---------------------------------------------------------------- write
    def put_many(self, items: list[tuple[str, dict, str]]) -> int:
        """Store ``(collection, doc, mode)`` tuples atomically. Returns rows newly added.

        ``mode='insert'`` - first write wins (immutable observations);
        ``mode='upsert'`` - last write wins (device/experiment metadata).
        """
        now = time.time()
        added = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for collection, doc, mode in items:
                    if "_id" not in doc:
                        raise ValueError("documents must carry a deterministic _id")
                    verb = "INSERT OR IGNORE" if mode == "insert" else "INSERT OR REPLACE"
                    cur.execute(f"{verb} INTO outbox(collection,id,mode,doc,created_at) VALUES (?,?,?,?,?)",
                                (collection, str(doc["_id"]), mode, dumps(doc), now))
                    added += cur.rowcount if cur.rowcount > 0 else 0
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            self._enforce_limit_locked()
        if self.archive is not None and items:
            try:
                self.archive.write_many(items)
            except Exception:  # noqa: BLE001 - the archive must never block the upload path
                log.exception("could not write records to the local archive")
        return added

    def put(self, collection: str, doc: dict, mode: str = "insert") -> int:
        return self.put_many([(collection, doc, mode)])

    def _enforce_limit_locked(self) -> None:
        total = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        excess = total - self.max_rows
        for coll in PRUNE_ORDER:
            if excess <= 0:
                break
            cur = self._conn.execute(
                "DELETE FROM outbox WHERE rowid IN (SELECT rowid FROM outbox WHERE collection=? "
                "ORDER BY created_at LIMIT ?)", (coll, excess))
            excess -= cur.rowcount
            self.pruned_rows += cur.rowcount
            if cur.rowcount:
                log.warning("buffer full: dropped oldest rows", extra={"collection": coll, "rows": cur.rowcount})

    # ----------------------------------------------------------------- read
    def fetch_batch(self, limit: int, exclude: tuple[str, ...] = ()) -> list[BufferedRow]:
        where = f"WHERE collection NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT collection,id,mode,doc,attempts FROM outbox {where} ORDER BY created_at, rowid LIMIT ?",
                (*exclude, limit)).fetchall()
        return [BufferedRow(c, i, m, loads(d), a) for c, i, m, d, a in rows]

    def delete(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.executemany("DELETE FROM outbox WHERE collection=? AND id=?", keys)
            self._conn.execute("COMMIT")

    def mark_failed(self, keys: list[tuple[str, str]], error: str) -> None:
        if not keys:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.executemany("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE collection=? AND id=?",
                                   [(error[:500], c, i) for c, i in keys])
            self._conn.execute("COMMIT")

    def move_to_dead_letter(self, items: list[tuple[str, str, str]]) -> None:
        """``(collection, id, error)`` rows that the server permanently rejected."""
        if not items:
            return
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            for coll, _id, err in items:
                row = self._conn.execute("SELECT doc FROM outbox WHERE collection=? AND id=?", (coll, _id)).fetchone()
                if row:
                    self._conn.execute("INSERT OR REPLACE INTO dead_letter VALUES (?,?,?,?,?)",
                                       (coll, _id, row[0], err[:2000], now))
                    self._conn.execute("DELETE FROM outbox WHERE collection=? AND id=?", (coll, _id))
            self._conn.execute("COMMIT")

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def count_by_collection(self) -> dict[str, int]:
        with self._lock:
            return dict(self._conn.execute("SELECT collection, COUNT(*) FROM outbox GROUP BY collection").fetchall())

    def dead_letter_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]

    def oldest_age_s(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(created_at) FROM outbox").fetchone()
        return (time.time() - row[0]) if row and row[0] else None

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
