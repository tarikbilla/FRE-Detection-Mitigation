"""Daily local backup of MongoDB (all collections of the configured databases).

Layout (one folder per backup, named by its UTC start timestamp, written
atomically via ``.partial`` + rename; the scheduler makes one per local day,
manual backups add more)::

    <backup_dir>/2026-11-24T033000Z/
        manifest.json                     counts, bytes, sha256, timings, redacted config
        fre_dataset_v3/telegrams.jsonl.gz MongoDB Extended JSON (relaxed), one doc per line

Only the application's own database (FRE_MONGODB_DB = fre_dataset_v3) is backed up.

Restore one collection::

    zcat telegrams.jsonl.gz | mongoimport --uri "$URI" -d fre_dataset_v3 -c telegrams

A collection whose (document count, data size) is unchanged since the previous
backup is hard-linked instead of downloaded again (no Atlas bandwidth, no disk). Backups older than
``backup_keep_days`` are deleted.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bson import json_util
from bson.json_util import JSONMode, JSONOptions

from . import __version__
from .config import Settings, redact_text

log = logging.getLogger(__name__)

JSON_OPTS = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True)
SYSTEM_PREFIXES = ("system.", "_healthcheck_")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_backups(cfg: Settings) -> list[dict]:
    """Completed backups, newest first (reads each manifest)."""
    root = cfg.backup_path
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if d.is_dir() and not d.name.endswith(".partial") and mf.is_file():
            try:
                m = json.loads(mf.read_text())
            except (OSError, ValueError):
                continue
            out.append({"name": d.name, "day": m.get("day") or d.name[:10], "path": str(d), "ok": m.get("ok"),
                        "started_at": m.get("started_at"), "finished_at": m.get("finished_at"),
                        "trigger": m.get("trigger", "scheduled"),
                        "duration_s": m.get("duration_s"), "documents": m.get("documents"),
                        "bytes": m.get("bytes"), "databases": list(m.get("databases", {}))})
    return out


def stamp_name(ts: datetime) -> str:
    """Folder name of a backup: UTC start time, e.g. 2026-11-24T033000Z (sorts chronologically)."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def real_collections(db) -> list[str]:
    """Collection names without views (e.g. the ``timeline`` view)."""
    try:
        return list(db.list_collection_names(filter={"type": "collection"}))
    except Exception:  # noqa: BLE001 - mocks without filter support
        return [n for n in db.list_collection_names() if n != "timeline"]


def has_backup_for(cfg: Settings, day: str) -> bool:
    """True if a completed backup exists whose (local) day is ``day``."""
    return any(b["day"] == day and b.get("ok") for b in list_backups(cfg))


def _previous_manifest(root: Path, current: str) -> tuple[Path | None, dict]:
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        if d.name < current and not d.name.endswith(".partial") and (d / "manifest.json").is_file():
            try:
                return d, json.loads((d / "manifest.json").read_text())
            except (OSError, ValueError):
                continue
    return None, {}


def prune(cfg: Settings, today: date | None = None) -> list[str]:
    today = today or date.today()
    cutoff = (today - timedelta(days=cfg.backup_keep_days)).isoformat()
    removed = []
    root = cfg.backup_path
    if not root.is_dir():
        return removed
    for d in root.iterdir():
        name = d.name.removesuffix(".partial")
        if d.is_dir() and len(name) >= 10 and name[4] == "-" and name[:10] < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


def run_backup(cfg: Settings, client=None, day: str | None = None, now: datetime | None = None,
               trigger: str = "manual") -> dict:
    """Dump every configured database to ``<backup_dir>/<UTC timestamp>``. Returns the manifest."""
    from pymongo import MongoClient

    root = cfg.backup_path
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o750)
    now = now or datetime.now(timezone.utc)
    day = day or now.astimezone().date().isoformat()
    name = stamp_name(now)
    free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    if free_mb < cfg.backup_min_free_mb:
        raise RuntimeError(f"only {free_mb} MB free in {root} (< {cfg.backup_min_free_mb} MB); backup skipped")

    final = root / name
    work = root / f"{name}.partial"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    prev_dir, prev = _previous_manifest(root, name)

    own_client = client is None
    if own_client:
        client = MongoClient(cfg.mongodb_uri, appname="fre-platform-backup", tz_aware=True,
                             serverSelectionTimeoutMS=cfg.mongodb_timeout_ms, socketTimeoutMS=120_000)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    manifest: dict = {"name": name, "day": day, "trigger": trigger, "host": socket.gethostname(), "software_version": __version__,
                      "started_at": started.isoformat(), "format": "MongoDB Extended JSON (relaxed), gzip, 1 doc/line",
                      "config": {k: v for k, v in cfg.safe_dict().items()
                                 if k.startswith(("mongodb_db", "station", "dataset", "experiment", "relay_exp",
                                                  "ir_exp", "backup"))},
                      "databases": {}, "documents": 0, "bytes": 0, "linked": 0, "ok": False}
    try:
        for db_name in cfg.backup_db_list:
            db = client[db_name]
            names = [n for n in real_collections(db) if not n.startswith(SYSTEM_PREFIXES)]
            dbm: dict = {}
            (work / db_name).mkdir()
            for coll in sorted(names):
                try:
                    stats = db.command("collStats", coll)
                    count, size = int(stats.get("count", 0)), int(stats.get("size", 0))
                except Exception:  # noqa: BLE001 - server/mocks without collStats: always export
                    count, size = db[coll].estimated_document_count(), -1
                out = work / db_name / f"{coll}.jsonl.gz"
                old = (prev.get("databases", {}).get(db_name, {}).get(coll) or {})
                old_file = prev_dir / db_name / f"{coll}.jsonl.gz" if prev_dir else None
                if old_file and old_file.is_file() and size >= 0 \
                        and old.get("count") == count and old.get("size") == size:
                    os.link(old_file, out)  # unchanged since last backup: hard link, no download
                    entry = {**old, "linked_from": prev_dir.name}
                    manifest["linked"] += 1
                else:
                    n = 0
                    with gzip.open(out, "wt", encoding="utf-8", compresslevel=6) as fh:
                        for doc in db[coll].find({}, batch_size=1000).sort("_id", 1):
                            fh.write(json_util.dumps(doc, json_options=JSON_OPTS))
                            fh.write("\n")
                            n += 1
                    entry = {"count": count, "size": size, "exported": n, "sha256": _sha256(out)}
                entry["file_bytes"] = out.stat().st_size
                dbm[coll] = entry
                manifest["documents"] += entry.get("exported", entry.get("count", 0))
                manifest["bytes"] += entry["file_bytes"]
            manifest["databases"][db_name] = dbm
        manifest["ok"] = True
    except Exception as exc:
        manifest["error"] = redact_text(f"{type(exc).__name__}: {exc}")[:500]
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["duration_s"] = round(time.monotonic() - t0, 1)
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
        if own_client:
            client.close()
        if manifest["ok"]:
            if final.exists():
                shutil.rmtree(final)
            work.rename(final)
            manifest["removed_old"] = prune(cfg, date.fromisoformat(day))
            log.info("backup finished", extra={k: manifest[k] for k in ("name", "trigger", "documents", "bytes",
                                                                            "linked", "duration_s")})
        else:
            log.error("backup failed; partial folder kept for inspection",
                      extra={"path": str(work), "error": manifest.get("error")})
    return manifest


class BackupScheduler(threading.Thread):
    """Runs :func:`run_backup` once per local day at ``backup_time`` (and catches up after downtime)."""

    def __init__(self, cfg: Settings):
        super().__init__(name="backup", daemon=True)
        self.cfg = cfg
        self._stop_evt = threading.Event()
        self.state = {"running": False, "last_result": None, "last_error": None, "next_run": None}

    def _due(self, now: datetime) -> bool:
        hh, mm = map(int, self.cfg.backup_time.split(":"))
        if has_backup_for(self.cfg, now.date().isoformat()):
            return False
        return (now.hour, now.minute) >= (hh, mm)

    def _next_run(self, now: datetime) -> str:
        hh, mm = map(int, self.cfg.backup_time.split(":"))
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if has_backup_for(self.cfg, now.date().isoformat()):
            nxt += timedelta(days=1)  # today's backup exists -> tomorrow
        elif nxt <= now:
            return "now"
        return nxt.isoformat(timespec="minutes")

    def run(self) -> None:
        failures = 0
        while not self._stop_evt.is_set():
            now = datetime.now().astimezone()
            self.state["next_run"] = self._next_run(now)
            if self._due(now):
                self.state["running"] = True
                try:
                    m = run_backup(self.cfg, trigger="scheduled")
                    self.state.update(last_result={k: m[k] for k in ("name", "documents", "bytes", "duration_s")},
                                      last_error=None)
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - retry later, never crash the service
                    failures += 1
                    self.state["last_error"] = redact_text(f"{type(exc).__name__}: {exc}")[:300]
                    log.exception("daily backup failed; retrying in %d min", min(60, 10 * failures))
                    self._stop_evt.wait(min(3600, 600 * failures))
                finally:
                    self.state["running"] = False
                continue
            self._stop_evt.wait(60)

    def stop(self) -> None:
        self._stop_evt.set()
