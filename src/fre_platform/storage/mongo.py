"""MongoDB Atlas access: schema, indexes and idempotent batch uploads."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReplaceOne, UpdateOne
from pymongo.errors import (BulkWriteError, CollectionInvalid, ConfigurationError, ConnectionFailure,
                            ExecutionTimeout, OperationFailure, PyMongoError, WTimeoutError)

from .. import SCHEMA_VERSION
from ..config import ConfigError, Settings, redact_text

log = logging.getLogger(__name__)

_INT = {"bsonType": ["int", "long"]}
_DATE = {"bsonType": "date"}
_STR = {"bsonType": "string"}
_COMMON_REQ = ["schema_version", "dataset_version", "station_id", "ts"]
_COMMON_PROPS = {"schema_version": _INT, "dataset_version": _STR, "station_id": _STR, "ts": _DATE,
                 "ts_ns": _INT, "experiment_id": _STR, "device_id": _STR, "source": _STR}

VALIDATORS: dict[str, dict] = {
    "raw_observations": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["quality", "psd", "center_freq_hz", "sample_rate_hz"],
        "properties": {**_COMMON_PROPS, "quality": {"bsonType": "object"}, "psd": {"bsonType": "object"},
                       "center_freq_hz": {"bsonType": ["double", "int", "long"]}, "sample_rate_hz": _INT}}},
    "measurements": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["channel", "features", "detection"],
        "properties": {**_COMMON_PROPS, "channel": {"bsonType": "object", "required": ["name", "freq_hz"]},
                       "features": {"bsonType": "object"}, "detection": {"bsonType": "object"},
                       "label": {"bsonType": ["string", "null"]}}}},
    "detections": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["channel", "verdict", "rule_hits"],
        "properties": {**_COMMON_PROPS, "verdict": {"enum": ["normal", "suspicious", "anomalous"]},
                       "rule_hits": {"bsonType": "array"}, "mitigations": {"bsonType": "array"}}}},
    "devices": {"$jsonSchema": {"bsonType": "object", "required": ["schema_version", "station_id", "device_type"],
                                "properties": {"schema_version": _INT, "station_id": _STR, "device_type": _STR}}},
    "experiments": {"$jsonSchema": {"bsonType": "object", "required": ["schema_version", "station_id", "conditions"],
                                    "properties": {"schema_version": _INT, "conditions": {"bsonType": "object"}}}},
    "datasets": {"$jsonSchema": {"bsonType": "object", "required": ["schema_version", "feature_names"],
                                 "properties": {"schema_version": _INT, "feature_names": {"bsonType": "array"}}}},
    "models": {"$jsonSchema": {"bsonType": "object", "required": ["schema_version", "metadata"],
                               "properties": {"schema_version": _INT, "metadata": {"bsonType": "object"}}}},
}

INDEXES: dict[str, list[IndexModel]] = {
    "raw_observations": [
        IndexModel([("station_id", ASCENDING), ("ts", DESCENDING)], name="station_ts"),
        IndexModel([("dataset_version", ASCENDING), ("ts", DESCENDING)], name="dataset_ts"),
    ],
    "measurements": [
        IndexModel([("station_id", ASCENDING), ("channel.name", ASCENDING), ("ts", DESCENDING)], name="station_channel_ts"),
        IndexModel([("dataset_version", ASCENDING), ("label", ASCENDING)], name="dataset_label"),
        IndexModel([("experiment_id", ASCENDING), ("ts", DESCENDING)], name="experiment_ts"),
        IndexModel([("detection.verdict", ASCENDING), ("ts", DESCENDING)], name="verdict_ts"),
    ],
    "detections": [
        IndexModel([("ts", DESCENDING)], name="ts"),
        IndexModel([("station_id", ASCENDING), ("channel.name", ASCENDING), ("ts", DESCENDING)], name="station_channel_ts"),
        IndexModel([("verdict", ASCENDING), ("ts", DESCENDING)], name="verdict_ts"),
        IndexModel([("rule_hits.code", ASCENDING)], name="rule_code"),
    ],
    "devices": [IndexModel([("station_id", ASCENDING)], name="station")],
    "experiments": [IndexModel([("station_id", ASCENDING), ("started_at", DESCENDING)], name="station_started")],
    "datasets": [IndexModel([("created_at", DESCENDING)], name="created")],
    "models": [IndexModel([("metadata.trained_at", DESCENDING)], name="trained_at")],
    "healthchecks": [IndexModel([("created_at", ASCENDING)], name="ttl_created", expireAfterSeconds=86400)],
}
COLLECTIONS = tuple(INDEXES)

# Server error codes that will never succeed on retry -> dead-letter the document.
PERMANENT_WRITE_CODES = {121, 2, 9, 14, 17280, 10334}
DUPLICATE_KEY = 11000


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionFailure, ExecutionTimeout, WTimeoutError)):
        return True
    if isinstance(exc, ConfigurationError):
        # e.g. SRV DNS lookup failing while offline
        return "dns" in str(exc).lower() or "resolution" in str(exc).lower()
    if isinstance(exc, PyMongoError) and hasattr(exc, "has_error_label"):
        return exc.has_error_label("RetryableWriteError") or exc.has_error_label("TransientTransactionError")
    return False


@dataclass
class UploadResult:
    ok: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


class MongoStore:
    def __init__(self, uri: str | None = None, db_name: str = "fre_research", app_name: str = "fre-platform",
                 timeout_ms: int = 8000, client: MongoClient | None = None):
        if client is None:
            if not uri:
                raise ConfigError("FRE_MONGODB_URI is not set")
            client = MongoClient(uri, appname=app_name, serverSelectionTimeoutMS=timeout_ms,
                                 connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms * 4,
                                 retryWrites=True, retryReads=True, tz_aware=True, w="majority")
        self.client = client
        self.db = client[db_name]
        self._schema_ready = False

    @classmethod
    def from_config(cls, cfg: Settings) -> "MongoStore":
        return cls(cfg.mongodb_uri, cfg.mongodb_db, cfg.mongodb_app_name, cfg.mongodb_timeout_ms)

    # --------------------------------------------------------------- admin
    def ping(self) -> dict:
        t = time.perf_counter()
        self.client.admin.command("ping")
        latency = (time.perf_counter() - t) * 1000
        try:
            version = self.client.server_info().get("version")
        except Exception:  # pragma: no cover - mongomock / restricted users
            version = None
        return {"ok": True, "latency_ms": round(latency, 1), "server_version": version}

    def ensure_schema(self, validators: bool = True) -> dict:
        report = {"created": [], "validator_updated": [], "validator_skipped": [], "indexes": {}}
        existing = set(self.db.list_collection_names())
        for name in COLLECTIONS:
            spec = VALIDATORS.get(name) if validators else None
            if name not in existing:
                try:
                    if spec:
                        self.db.create_collection(name, validator=spec, validationLevel="moderate",
                                                  validationAction="error")
                    else:
                        self.db.create_collection(name)
                    report["created"].append(name)
                except CollectionInvalid:
                    pass  # created concurrently
                except (OperationFailure, NotImplementedError, TypeError) as exc:
                    log.warning("could not create %s with validator (%s); creating without", name, exc)
                    try:
                        self.db.create_collection(name)
                        report["created"].append(name)
                    except CollectionInvalid:
                        pass
                    report["validator_skipped"].append(name)
            elif spec:
                try:
                    self.db.command({"collMod": name, "validator": spec, "validationLevel": "moderate"})
                    report["validator_updated"].append(name)
                except (OperationFailure, NotImplementedError, TypeError) as exc:
                    log.warning("could not update validator on %s: %s", name, exc)
                    report["validator_skipped"].append(name)
            names = self.db[name].create_indexes(INDEXES[name])
            report["indexes"][name] = names
        self._schema_ready = True
        return report

    # --------------------------------------------------------------- write
    def upload(self, collection: str, rows: list[tuple[str, str, dict]]) -> UploadResult:
        """Idempotently write ``(id, mode, doc)`` rows. Transient errors propagate."""
        res = UploadResult()
        if not rows:
            return res
        ops = []
        for _id, mode, doc in rows:
            body = {k: v for k, v in doc.items() if k != "_id"}
            if mode == "insert":
                ops.append(UpdateOne({"_id": _id}, {"$setOnInsert": body}, upsert=True))
            else:
                ops.append(ReplaceOne({"_id": _id}, body, upsert=True))
        try:
            self.db[collection].bulk_write(ops, ordered=False)
            res.ok = [r[0] for r in rows]
            return res
        except BulkWriteError as exc:
            details = exc.details or {}
            if details.get("writeConcernErrors"):
                raise
            failed: dict[int, dict] = {e["index"]: e for e in details.get("writeErrors", [])}
            for idx, (_id, _mode, _doc) in enumerate(rows):
                err = failed.get(idx)
                if err is None or err.get("code") == DUPLICATE_KEY:
                    res.ok.append(_id)
                elif err.get("code") in PERMANENT_WRITE_CODES:
                    res.rejected.append((_id, redact_text(f"{err.get('code')}: {err.get('errmsg')}")))
                else:
                    raise
            return res

    def healthcheck_roundtrip(self, station_id: str) -> dict:
        coll = self.db["healthchecks"]
        doc_id = f"{station_id}|{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        t = time.perf_counter()
        coll.insert_one({"_id": doc_id, "station_id": station_id, "created_at": now, "schema_version": SCHEMA_VERSION})
        back = coll.find_one({"_id": doc_id})
        coll.delete_one({"_id": doc_id})
        gone = coll.find_one({"_id": doc_id}) is None
        return {"insert_read_delete_ok": bool(back and back["station_id"] == station_id and gone),
                "roundtrip_ms": round((time.perf_counter() - t) * 1000, 1)}

    def counts(self) -> dict[str, int]:
        return {c: self.db[c].estimated_document_count() for c in COLLECTIONS}

    def close(self) -> None:
        self.client.close()
