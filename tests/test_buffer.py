from datetime import datetime, timezone

import pytest

from fre_platform.storage.buffer import LocalBuffer


def doc(i, **kw):
    return {"_id": f"id{i}", "ts": datetime(2026, 11, 14, 1, 2, 3, 456000, tzinfo=timezone.utc), "v": i, **kw}


def test_insert_is_idempotent(tmp_path):
    b = LocalBuffer(tmp_path / "b.sqlite3")
    assert b.put_many([("measurements", doc(1), "insert"), ("measurements", doc(2), "insert")]) == 2
    assert b.put("measurements", doc(1, v=999)) == 0  # duplicate ignored, first write wins
    rows = b.fetch_batch(10)
    assert [r.id for r in rows] == ["id1", "id2"] and rows[0].doc["v"] == 1


def test_upsert_last_write_wins(tmp_path):
    b = LocalBuffer(tmp_path / "b.sqlite3")
    b.put("devices", {"_id": "dev", "x": 1}, "upsert")
    b.put("devices", {"_id": "dev", "x": 2}, "upsert")
    rows = b.fetch_batch(10)
    assert len(rows) == 1 and rows[0].doc["x"] == 2


def test_datetime_roundtrip_is_tz_aware(tmp_path):
    b = LocalBuffer(tmp_path / "b.sqlite3")
    b.put("measurements", doc(1))
    ts = b.fetch_batch(1)[0].doc["ts"]
    assert ts.tzinfo is not None and ts == doc(1)["ts"]


def test_survives_reopen(tmp_path):
    p = tmp_path / "b.sqlite3"
    b = LocalBuffer(p)
    b.put("measurements", doc(1))
    b.close()
    assert LocalBuffer(p).count() == 1


def test_delete_and_dead_letter(tmp_path):
    b = LocalBuffer(tmp_path / "b.sqlite3")
    b.put_many([("measurements", doc(i), "insert") for i in range(3)])
    b.delete([("measurements", "id0")])
    b.move_to_dead_letter([("measurements", "id1", "121: validation failed")])
    assert b.count() == 1 and b.dead_letter_count() == 1


def test_prune_drops_raw_first_never_detections(tmp_path):
    b = LocalBuffer(tmp_path / "b.sqlite3", max_rows=5)
    b.put_many([("detections", doc(f"d{i}"), "insert") for i in range(3)])
    b.put_many([("raw_observations", doc(f"r{i}"), "insert") for i in range(4)])
    counts = b.count_by_collection()
    assert counts["detections"] == 3 and counts["raw_observations"] == 2 and b.pruned_rows == 2


def test_requires_id(tmp_path):
    with pytest.raises(ValueError):
        LocalBuffer(tmp_path / "b.sqlite3").put("measurements", {"x": 1})
