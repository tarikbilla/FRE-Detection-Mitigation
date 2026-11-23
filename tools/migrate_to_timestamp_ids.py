#!/usr/bin/env python3
"""One-off migration to timestamp-based storage (platform v2.2.0). Idempotent.

1. MongoDB (active database only, FRE_MONGODB_DB): documents whose _id is not yet
   timestamp-first get the new id ``<UTC ts>|<station>|...`` (insert new, delete old).
   ``sessions`` keep their experiment id. The legacy ``fre_dataset`` is NOT touched.
2. Local archive: backfills every document of the active database into
   ``<archive_dir>/YYYY/MM/DD/<collection>.jsonl`` by its timestamp - only if the
   archive is still empty (so running it twice never duplicates).
3. Backups: day-named folders (``2026-11-23``) are renamed to their UTC start
   timestamp (``2026-11-23T150107Z``).

Run with the service STOPPED (so no old-format ids are still buffered):

    sudo systemctl stop fre-platform
    FRE_ENV_FILE=/etc/fre-platform/fre.env .venv/bin/python tools/migrate_to_timestamp_ids.py [--dry-run]
    sudo systemctl start fre-platform
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pymongo import DeleteOne, MongoClient, UpdateOne  # noqa: E402

from fre_platform.archive import TimeArchive  # noqa: E402
from fre_platform.backup import stamp_name  # noqa: E402
from fre_platform.config import Settings  # noqa: E402
from fre_platform.timeid import make_id  # noqa: E402

NEW_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\|")
COLLECTIONS = ("telegrams", "observations", "spectra", "relay_events", "ir_readouts")


def new_id(coll: str, doc: dict) -> str | None:
    ts = doc.get("ts")
    if not isinstance(ts, datetime):
        return None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    old = str(doc["_id"])
    if coll in ("telegrams", "observations", "spectra"):
        parts = old.split("|")  # station | ts_ns | channel-or-'spectrum' [| index]
        return make_id(ts, parts[0], *parts[2:]) if len(parts) >= 3 else None
    if coll == "relay_events":
        ts_utc = ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        key = f'{doc.get("host")}|{ts_utc}|{doc.get("type")}|{doc.get("raw", "")}'
        return make_id(ts, doc.get("host"), "relay", doc.get("type"), hashlib.sha1(key.encode()).hexdigest()[:10])
    if coll == "ir_readouts":
        return make_id(ts, doc.get("station_id"), "ir")
    return None


def migrate_ids(db, dry: bool) -> dict:
    out = {}
    for coll in COLLECTIONS:
        ops, n = [], 0
        for doc in db[coll].find({}):
            if NEW_ID.match(str(doc["_id"])):
                continue
            nid = new_id(coll, doc)
            if not nid:
                continue
            body = {k: v for k, v in doc.items() if k != "_id"}
            ops += [UpdateOne({"_id": nid}, {"$setOnInsert": body}, upsert=True), DeleteOne({"_id": doc["_id"]})]
            n += 1
            if len(ops) >= 1000 and not dry:
                db[coll].bulk_write(ops, ordered=True)
                ops = []
        if ops and not dry:
            db[coll].bulk_write(ops, ordered=True)
        out[coll] = n
    return out


def backfill_archive(db, cfg: Settings, dry: bool) -> dict:
    root = cfg.archive_path
    if root.is_dir() and any(root.rglob("*.jsonl*")):
        return {"skipped": f"{root} already contains records"}
    arc = TimeArchive(root)
    out = {}
    for coll in sorted(db.list_collection_names(filter={"type": "collection"})):
        docs = list(db[coll].find({}).sort("_id", 1))
        out[coll] = len(docs)
        if not dry and docs:
            arc.write_many([(coll, d, "insert") for d in docs])
    arc.close()
    return out


def rename_backups(cfg: Settings, dry: bool) -> list[str]:
    done = []
    root = cfg.backup_path
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        mf = d / "manifest.json"
        if not (d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and mf.is_file()):
            continue
        m = json.loads(mf.read_text())
        name = stamp_name(datetime.fromisoformat(m["started_at"]))
        m.update(name=name, day=m.get("day") or d.name, trigger=m.get("trigger", "scheduled"))
        if not dry:
            mf.write_text(json.dumps(m, indent=2, default=str))
            d.rename(root / name)
        done.append(f"{d.name} -> {name}")
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = Settings.from_env()
    client = MongoClient(cfg.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=15000)
    db = client[cfg.mongodb_db]
    print(f"database: {cfg.mongodb_db}{'  (DRY RUN)' if args.dry_run else ''}")
    print("ids migrated:", migrate_ids(db, args.dry_run))
    print("archive backfilled:", backfill_archive(db, cfg, args.dry_run))
    print("backups renamed:", rename_backups(cfg, args.dry_run))
    left = {c: db[c].count_documents({"_id": {"$not": NEW_ID}}) for c in COLLECTIONS}
    print("documents still in old id format:", left)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
