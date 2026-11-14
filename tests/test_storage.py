"""MongoStore + Uploader behaviour, including offline buffering and recovery."""
from datetime import datetime, timezone

import pytest
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from fre_platform.storage.buffer import LocalBuffer
from fre_platform.storage.mongo import COLLECTIONS, UploadResult, is_transient
from fre_platform.storage.uploader import Uploader


def mdoc(i):
    return {"_id": f"m{i}", "schema_version": 1, "ts": datetime.now(timezone.utc), "v": i}


def test_ensure_schema_creates_collections_and_indexes(mongomock_store):
    report = mongomock_store.ensure_schema()
    assert set(COLLECTIONS) <= set(mongomock_store.db.list_collection_names())
    idx = mongomock_store.db.measurements.index_information()
    assert "station_channel_ts" in idx and "dataset_label" in idx
    assert "ttl_created" in mongomock_store.db.healthchecks.index_information()
    assert report["indexes"]["detections"]
    mongomock_store.ensure_schema()  # idempotent


def test_upload_is_idempotent(mongomock_store):
    rows = [(f"m{i}", "insert", mdoc(i)) for i in range(5)]
    assert len(mongomock_store.upload("measurements", rows).ok) == 5
    assert len(mongomock_store.upload("measurements", rows).ok) == 5  # re-send after lost ack
    assert mongomock_store.db.measurements.count_documents({}) == 5


def test_upsert_mode_replaces(mongomock_store):
    mongomock_store.upload("devices", [("d", "upsert", {"_id": "d", "x": 1})])
    mongomock_store.upload("devices", [("d", "upsert", {"_id": "d", "x": 2})])
    assert mongomock_store.db.devices.find_one({"_id": "d"})["x"] == 2


def test_healthcheck_roundtrip(mongomock_store):
    assert mongomock_store.healthcheck_roundtrip("st")["insert_read_delete_ok"]


def test_transient_classification():
    assert is_transient(AutoReconnect("x")) and is_transient(ServerSelectionTimeoutError("x"))
    assert not is_transient(ValueError("x"))


class FlakyStore:
    """Wraps a real (mongomock) store and fails the first ``fail`` operations."""

    def __init__(self, inner, fail):
        self.inner, self.fail, self.calls = inner, fail, 0

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.fail:
            raise AutoReconnect("simulated network outage")

    def ping(self):
        self._maybe_fail()
        return self.inner.ping()

    def ensure_schema(self):
        return self.inner.ensure_schema()

    def upload(self, coll, rows):
        self._maybe_fail()
        return self.inner.upload(coll, rows)

    def close(self):
        pass


def test_offline_buffering_and_recovery(cfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    buf.put_many([("measurements", mdoc(i), "insert") for i in range(120)])
    flaky = FlakyStore(mongomock_store, fail=3)
    up = Uploader(buf, lambda: flaky, cfg)

    # outage: every attempt fails, nothing is lost
    for _ in range(3):
        assert up.flush_once() is False
        assert buf.count() == 120
    assert up.stats["consecutive_failures"] == 3 and not up.stats["online"]
    assert up.stats["backoff_s"] > cfg.upload_interval_s

    # recovery: everything uploaded exactly once, buffer drained
    assert up.flush_once() is True
    assert buf.count() == 0 and up.stats["online"]
    assert mongomock_store.db.measurements.count_documents({}) == 120

    # the same documents buffered again (e.g. crash before delete) do not duplicate
    buf.put_many([("measurements", mdoc(i), "insert") for i in range(10)])
    assert up.flush_once() and mongomock_store.db.measurements.count_documents({}) == 120


def test_permanent_rejection_goes_to_dead_letter(cfg, tmp_path, mongomock_store):
    class RejectingStore(FlakyStore):
        def upload(self, coll, rows):
            return UploadResult(ok=[r[0] for r in rows[1:]], rejected=[(rows[0][0], "121: Document failed validation")])

    buf = LocalBuffer(tmp_path / "b.sqlite3")
    buf.put_many([("measurements", mdoc(i), "insert") for i in range(3)])
    up = Uploader(buf, lambda: RejectingStore(mongomock_store, 0), cfg)
    assert up.flush_once()
    assert buf.count() == 0 and buf.dead_letter_count() == 1 and up.stats["rejected"] == 1


def test_background_thread_uploads(cfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, cfg)
    up.start()
    buf.put_many([("measurements", mdoc(i), "insert") for i in range(30)])
    up.trigger()
    up.stop(final_flush_timeout=10)
    assert buf.count() == 0
    assert mongomock_store.db.measurements.count_documents({}) == 30
