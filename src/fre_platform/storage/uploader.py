"""Background uploader: drains the local buffer into MongoDB in batches.

* Batches are grouped per collection and written with idempotent upserts.
* Rows are removed from the buffer only after the server acknowledged them.
* Network / Atlas outages -> exponential backoff, data stays buffered.
* Documents the server permanently rejects (schema validation) are moved
  to the ``dead_letter`` table for inspection instead of blocking the queue.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from ..config import ConfigError, Settings, redact_text
from .buffer import LocalBuffer
from .mongo import MongoStore, is_transient

log = logging.getLogger(__name__)


class Uploader(threading.Thread):
    def __init__(self, buffer: LocalBuffer, store_factory: Callable[[], MongoStore], cfg: Settings):
        super().__init__(name="uploader", daemon=True)
        self.buffer = buffer
        self.store_factory = store_factory
        self.cfg = cfg
        self.store: MongoStore | None = None
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._flush_lock = threading.Lock()
        self.stats = {"uploaded": 0, "rejected": 0, "failures": 0, "consecutive_failures": 0, "online": False,
                      "last_success": None, "last_error": None, "backoff_s": cfg.upload_interval_s,
                      "storage_used_mb": None, "quota_restricted": False}
        self._last_quota_check = float("-inf")

    # ------------------------------------------------------------------
    BULK_COLLECTIONS = ("raw_observations", "measurements")

    def _check_quota(self, store: MongoStore) -> None:
        """Free-tier guard: near the Atlas quota, keep bulk data local and upload only
        detections and metadata (bulk rows stay buffered / are pruned locally)."""
        if time.monotonic() - self._last_quota_check < self.cfg.quota_check_s:
            return
        self._last_quota_check = time.monotonic()
        used_fn = getattr(store, "storage_used_mb", None)
        used = used_fn() if used_fn else None
        self.stats["storage_used_mb"] = used
        if used is None:
            return
        limit = self.cfg.atlas_quota_mb * self.cfg.atlas_soft_limit_pct / 100
        restricted = used >= limit
        if restricted != self.stats["quota_restricted"]:
            log.warning("Atlas storage %s soft limit: %s", "above" if restricted else "back below",
                        "uploading detections/metadata only" if restricted else "resuming full upload",
                        extra={"used_mb": used, "limit_mb": round(limit, 1), "quota_mb": self.cfg.atlas_quota_mb})
        self.stats["quota_restricted"] = restricted

    # ------------------------------------------------------------------
    def _ensure_store(self) -> MongoStore:
        if self.store is None:
            store = self.store_factory()
            store.ping()
            store.ensure_schema()
            self.store = store
            log.info("connected to MongoDB", extra={"db": self.cfg.mongodb_db})
        return self.store

    def flush_once(self, max_batches: int = 50) -> bool:
        """Upload up to ``max_batches`` batches. Returns False on a (transient) failure."""
        with self._flush_lock:
            try:
                store = self._ensure_store()
                self._check_quota(store)
                exclude = self.BULK_COLLECTIONS if self.stats["quota_restricted"] else ()
                for _ in range(max_batches):
                    rows = self.buffer.fetch_batch(self.cfg.batch_size, exclude=exclude)
                    if not rows:
                        break
                    groups: dict[str, list] = defaultdict(list)
                    for r in rows:
                        groups[r.collection].append(r)
                    for coll, items in groups.items():
                        res = store.upload(coll, [(r.id, r.mode, r.doc) for r in items])
                        self.buffer.delete([(coll, i) for i in res.ok])
                        if res.rejected:
                            self.buffer.move_to_dead_letter([(coll, i, e) for i, e in res.rejected])
                            self.stats["rejected"] += len(res.rejected)
                            log.error("documents rejected by server (moved to dead letter)",
                                      extra={"collection": coll, "count": len(res.rejected),
                                             "first_error": res.rejected[0][1]})
                        self.stats["uploaded"] += len(res.ok)
                    if len(rows) < self.cfg.batch_size:
                        break
                self._mark_ok()
                return True
            except ConfigError as exc:
                self._mark_fail(exc, level=logging.ERROR)
                return False
            except Exception as exc:  # noqa: BLE001 - keep data, report, retry later
                if is_transient(exc):
                    self._mark_fail(exc, level=logging.WARNING)
                else:
                    self._mark_fail(exc, level=logging.ERROR, with_trace=True)
                    # drop the client so the next attempt reconnects from scratch
                    if self.store is not None:
                        try:
                            self.store.close()
                        except Exception:  # pragma: no cover
                            pass
                        self.store = None
                return False

    def _mark_ok(self) -> None:
        if not self.stats["online"] and self.stats["consecutive_failures"]:
            log.info("MongoDB reachable again; buffered data uploaded",
                     extra={"after_failures": self.stats["consecutive_failures"]})
        self.stats.update(online=True, consecutive_failures=0, backoff_s=self.cfg.upload_interval_s,
                          last_success=datetime.now(timezone.utc).isoformat())

    def _mark_fail(self, exc: BaseException, level: int, with_trace: bool = False) -> None:
        self.stats["failures"] += 1
        self.stats["consecutive_failures"] += 1
        self.stats["online"] = False
        msg = redact_text(f"{type(exc).__name__}: {exc}")[:500]
        self.stats["last_error"] = msg
        self.stats["backoff_s"] = min(self.cfg.upload_interval_s * 2 ** min(self.stats["consecutive_failures"], 10),
                                      self.cfg.max_backoff_s)
        log.log(level, "upload failed; data kept in local buffer",
                extra={"error": msg, "buffered": self.buffer.count(), "retry_in_s": self.stats["backoff_s"]},
                exc_info=with_trace)

    # ------------------------------------------------------------------
    def run(self) -> None:
        while not self._stop_evt.is_set():
            ok = self.flush_once()
            wait = self.cfg.upload_interval_s if ok else self.stats["backoff_s"]
            self._wake.wait(wait)
            self._wake.clear()

    def trigger(self) -> None:
        self._wake.set()

    def stop(self, final_flush_timeout: float = 20.0) -> None:
        self._stop_evt.set()
        self._wake.set()
        if self.is_alive():
            self.join(timeout=final_flush_timeout)
        deadline = time.monotonic() + final_flush_timeout
        while self.buffer.count() and time.monotonic() < deadline:
            if not self.flush_once():
                break
        if self.store is not None:
            self.store.close()
            self.store = None
