"""Real MongoDB Atlas integration test (opt-in).

Run with:  FRE_RUN_ATLAS_TESTS=1 FRE_ENV_FILE=/etc/fre-platform/fre.env pytest -m atlas
Uses a separate ``<FRE_MONGODB_DB>_test`` database and drops it afterwards.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.atlas,
              pytest.mark.skipif(os.environ.get("FRE_RUN_ATLAS_TESTS") != "1", reason="set FRE_RUN_ATLAS_TESTS=1")]


@pytest.fixture
def atlas_store():
    from fre_platform.config import Settings
    from fre_platform.storage.mongo import MongoStore
    cfg = Settings.from_env()
    if not cfg.mongodb_uri:
        pytest.skip("FRE_MONGODB_URI not configured")
    db = f"{cfg.mongodb_db}_test"
    store = MongoStore(cfg.mongodb_uri, db_name=db, timeout_ms=15000)
    yield store
    store.client.drop_database(db)
    store.close()


def test_atlas_ping_schema_roundtrip(atlas_store):
    assert atlas_store.ping()["ok"]
    report = atlas_store.ensure_schema()
    assert "measurements" in atlas_store.db.list_collection_names()
    assert report["indexes"]["measurements"]
    assert atlas_store.healthcheck_roundtrip("pytest")["insert_read_delete_ok"]


def test_atlas_idempotent_insert_and_read(atlas_store):
    atlas_store.ensure_schema()
    now = datetime.now(timezone.utc)
    rid = f"pytest|{uuid.uuid4().hex}"
    doc = {"_id": rid, "schema_version": 1, "dataset_version": "test", "station_id": "pytest", "ts": now,
           "channel": {"name": "T", "freq_hz": 1.0}, "features": {"snr_db": 1.0},
           "detection": {"verdict": "normal"}, "label": None}
    for _ in range(2):
        assert atlas_store.upload("measurements", [(rid, "insert", doc)]).ok == [rid]
    assert atlas_store.db.measurements.count_documents({"_id": rid}) == 1
    back = atlas_store.db.measurements.find_one({"_id": rid})
    assert back["features"]["snr_db"] == 1.0 and back["ts"].tzinfo is not None


def test_atlas_schema_validation_rejects_bad_docs(atlas_store):
    atlas_store.ensure_schema()
    res = atlas_store.upload("measurements", [("bad", "insert", {"_id": "bad", "nonsense": True})])
    assert res.rejected and res.rejected[0][0] == "bad"
