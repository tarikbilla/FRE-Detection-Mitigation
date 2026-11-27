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
_NUM = {"bsonType": ["double", "int", "long"]}
_DATE = {"bsonType": "date"}
_STR = {"bsonType": "string"}
_BOOL = {"bsonType": "bool"}
_CHANNEL = {"bsonType": "object", "required": ["name", "freq_hz"],
            "properties": {"name": _STR, "freq_hz": _NUM}}
# Fields shared by every time-series research record.
_COMMON_REQ = ["schema_version", "dataset_version", "experiment_id", "station_id", "ts"]
_COMMON_PROPS = {"schema_version": _INT, "dataset_version": _STR, "experiment_id": _STR, "station_id": _STR,
                 "ts": _DATE, "ts_ns": _INT, "seq": _INT, "source": _STR}

# Clean, professional research dataset: one metadata collection, three SDR
# time-series collections (signal features, spectra, decoded telegrams) and two
# FRE-side collections (relay switching ground truth, optical-port probes). ML /
# detection results are NOT stored here - they are added in a later phase.
VALIDATORS: dict[str, dict] = {
    # One document per collection run / configuration (reproducibility metadata).
    "sessions": {"$jsonSchema": {
        "bsonType": "object", "required": ["schema_version", "dataset_version", "station_id", "device", "config"],
        "properties": {"schema_version": _INT, "dataset_version": _STR, "station_id": _STR,
                       "device": {"bsonType": "object"}, "config": {"bsonType": "object"},
                       "feature_names": {"bsonType": "array"}}}},
    # Per-channel signal measurements: the ML-ready feature time-series.
    "observations": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["channel", "features", "signal_state"],
        "properties": {**_COMMON_PROPS, "channel": _CHANNEL, "features": {"bsonType": "object"},
                       "signal_state": {"enum": ["present", "lost", "absent"]},
                       "quality": {"bsonType": "object"}}}},
    # Periodic band spectrum + block quality (frequency-domain research data).
    "spectra": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["center_freq_hz", "sample_rate_hz", "psd"],
        "properties": {**_COMMON_PROPS, "center_freq_hz": _NUM, "sample_rate_hz": _INT,
                       "psd": {"bsonType": "object"}, "band": {"bsonType": "object"},
                       "quality": {"bsonType": "object"}}}},
    # Decoded received FRE telegrams (the actual signal content).
    "telegrams": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["channel", "kind", "checksum_ok", "raw_hex"],
        "properties": {**_COMMON_PROPS, "channel": _CHANNEL, "kind": _STR, "protocol": _STR,
                       "checksum_ok": _BOOL, "raw_hex": _STR, "evu_address": {"bsonType": ["string", "null"]},
                       "time": {"bsonType": ["object", "null"]}}}},
    # Ground truth: what the FRE actually switched (ESP32 on the K1..K4 dry contacts).
    "relay_events": {"$jsonSchema": {
        "bsonType": "object", "required": _COMMON_REQ + ["type"],
        "properties": {**_COMMON_PROPS, "type": _STR, "relay": {"bsonType": ["string", "null"]},
                       "position": {"enum": ["a", "b", None]}, "esp_ms": {"bsonType": ["int", "long", "null"]},
                       "host": _STR, "raw": {"bsonType": ["string", "null"]}}}},
}

INDEXES: dict[str, list[IndexModel]] = {
    "sessions": [IndexModel([("station_id", ASCENDING), ("started_at", DESCENDING)], name="station_started"),
                 IndexModel([("dataset_version", ASCENDING)], name="dataset")],
    "observations": [
        IndexModel([("dataset_version", ASCENDING), ("channel.name", ASCENDING), ("ts", ASCENDING)], name="dataset_channel_ts"),
        IndexModel([("experiment_id", ASCENDING), ("seq", ASCENDING)], name="experiment_seq"),
        IndexModel([("station_id", ASCENDING), ("ts", DESCENDING)], name="station_ts"),
        IndexModel([("signal_state", ASCENDING), ("ts", DESCENDING)], name="signal_state_ts"),
    ],
    "spectra": [
        IndexModel([("dataset_version", ASCENDING), ("ts", ASCENDING)], name="dataset_ts"),
        IndexModel([("experiment_id", ASCENDING), ("seq", ASCENDING)], name="experiment_seq"),
    ],
    "telegrams": [
        IndexModel([("dataset_version", ASCENDING), ("ts", ASCENDING)], name="dataset_ts"),
        IndexModel([("channel.name", ASCENDING), ("ts", DESCENDING)], name="channel_ts"),
        IndexModel([("kind", ASCENDING), ("ts", DESCENDING)], name="kind_ts"),
        IndexModel([("evu_address", ASCENDING)], name="evu_address"),
    ],
    "relay_events": [
        IndexModel([("station_id", ASCENDING), ("ts", ASCENDING)], name="station_ts"),
        IndexModel([("type", ASCENDING), ("ts", DESCENDING)], name="type_ts"),
        IndexModel([("relay", ASCENDING), ("ts", DESCENDING)], name="relay_ts"),
    ],
}
COLLECTIONS = tuple(INDEXES)

# Read-only view: ONE time-ordered stream of what was broadcast (SDR telegrams), what
# the receiver did (relay events, heartbeats excluded). The IR optical-port probes
# (ir_readouts) were dropped on 27 Nov 2026 - the port never answered.
# No storage cost. Query:  db.timeline.find({ts: {$gte: ISODate("2026-11-24")}}).sort({ts: 1})
# (ids are timestamp-first, so .sort({_id: 1}) is chronological as well).
TIMELINE_VIEW = "timeline"
TIMELINE_PIPELINE = [
    {"$project": {"_id": 1, "ts": 1, "station_id": 1, "dataset_version": 1, "source": {"$literal": "sdr"},
                  "event": "$kind", "channel": "$channel.name", "assessment": "$integrity.assessment",
                  "clock_offset_s": "$integrity.clock_offset_s", "evu_address": 1, "checksum_ok": 1, "raw_hex": 1}},
    {"$unionWith": {"coll": "relay_events", "pipeline": [
        {"$match": {"type": {"$nin": ["state", "other"]}}},
        {"$project": {"_id": 1, "ts": 1, "station_id": 1, "dataset_version": 1, "source": {"$literal": "relay"},
                      "event": "$type", "relay": 1, "position": 1, "level_pct": 1, "esp_ms": 1, "detail": 1}}]}},
]

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
    def __init__(self, uri: str | None = None, db_name: str = "fre_dataset_v2", app_name: str = "fre-platform",
                 timeout_ms: int = 8000, client: MongoClient | None = None, reserved_mb: float = 0.0):
        if client is None:
            if not uri:
                raise ConfigError("FRE_MONGODB_URI is not set")
            client = MongoClient(uri, appname=app_name, serverSelectionTimeoutMS=timeout_ms,
                                 connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms * 4,
                                 retryWrites=True, retryReads=True, tz_aware=True, w="majority")
        self.client = client
        self.db = client[db_name]   # the only database this store ever touches
        self.reserved_mb = reserved_mb
        self._schema_ready = False

    @classmethod
    def from_config(cls, cfg: Settings) -> "MongoStore":
        return cls(cfg.mongodb_uri, cfg.mongodb_db, cfg.mongodb_app_name, cfg.mongodb_timeout_ms,
                   reserved_mb=cfg.atlas_reserved_mb)

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
        report["timeline_view"] = self.ensure_timeline_view(TIMELINE_VIEW in existing)
        self._schema_ready = True
        return report

    def ensure_timeline_view(self, exists: bool) -> str:
        try:
            if exists:
                self.db.command({"collMod": TIMELINE_VIEW, "viewOn": "telegrams", "pipeline": TIMELINE_PIPELINE})
                return "updated"
            self.db.command({"create": TIMELINE_VIEW, "viewOn": "telegrams", "pipeline": TIMELINE_PIPELINE})
            return "created"
        except (OperationFailure, NotImplementedError, TypeError) as exc:  # e.g. mongomock
            log.warning("could not create the timeline view: %s", exc)
            return "unsupported"

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
        """Insert/read/delete a document in a throwaway collection, then drop it.

        Leaves no residue in the research dataset (no dedicated health collection).
        """
        name = f"_healthcheck_{uuid.uuid4().hex[:8]}"
        coll = self.db[name]
        doc_id = f"{station_id}|{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        t = time.perf_counter()
        try:
            coll.insert_one({"_id": doc_id, "station_id": station_id, "created_at": now})
            back = coll.find_one({"_id": doc_id})
            coll.delete_one({"_id": doc_id})
            gone = coll.find_one({"_id": doc_id}) is None
        finally:
            coll.drop()
        return {"insert_read_delete_ok": bool(back and back["station_id"] == station_id and gone),
                "roundtrip_ms": round((time.perf_counter() - t) * 1000, 1)}

    def storage_used_mb(self) -> float | None:
        """Quota-relevant MB: this database (data + index) + ``reserved_mb``.

        The Atlas free-tier quota (M0, 512 MB) is per cluster. Other databases on the
        cluster (the frozen ``fre_dataset``) are accounted for by the configured
        ``FRE_ATLAS_RESERVED_MB`` - this application never connects to them.
        """
        try:
            s = self.db.command("dbStats")
            own = (s.get("dataSize", 0) + s.get("indexSize", 0)) / 1024 / 1024
            return round(own + self.reserved_mb, 2)
        except (OperationFailure, NotImplementedError, TypeError, KeyError):
            return None

    def counts(self) -> dict[str, int]:
        return {c: self.db[c].estimated_document_count() for c in COLLECTIONS}

    def close(self) -> None:
        self.client.close()
