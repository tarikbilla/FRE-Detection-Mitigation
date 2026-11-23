"""Local, timestamp-partitioned archive of EVERY record the platform produces.

Every document that enters the upload buffer (SDR telegrams/observations/spectra/
sessions, relay events, IR probes) is also appended here, filed by the record's
own UTC timestamp (``ts``), not by the time it was written::

    <archive_dir>/2026/11/23/telegrams.jsonl      today: plain JSONL, appended live
    <archive_dir>/2026/11/22/telegrams.jsonl.gz   past days: gzip-compressed

One MongoDB Extended JSON (relaxed) document per line, so a day can be replayed
into MongoDB (``mongoimport``) or read with pandas. The archive is the complete
local copy - it does not depend on the network, Atlas or the free-tier quota.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bson import json_util
from bson.json_util import JSONMode, JSONOptions

log = logging.getLogger(__name__)
JSON_OPTS = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True)


def record_time(doc: dict) -> datetime:
    """The record's own timestamp (UTC); falls back to metadata times, then now."""
    for key in ("ts", "updated_at", "started_at"):
        v = doc.get(key)
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class TimeArchive:
    def __init__(self, root, keep_days: int = 0):
        self.root = Path(root)
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._files: dict[Path, object] = {}
        self.written: dict[str, int] = {}

    def path_for(self, collection: str, ts: datetime) -> Path:
        d = ts.astimezone(timezone.utc)
        return self.root / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"{collection}.jsonl"

    def _handle(self, path: Path):
        fh = self._files.get(path)
        if fh is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._files) > 32:  # day rollover: close handles of older days
                self.close()
            fh = open(path, "a", encoding="utf-8")
            self._files[path] = fh
        return fh

    def write_many(self, items) -> None:
        """``items``: iterable of (collection, doc, mode) as given to the buffer."""
        with self._lock:
            touched = set()
            for collection, doc, _mode in items:
                path = self.path_for(collection, record_time(doc))
                fh = self._handle(path)
                fh.write(json_util.dumps({"_collection": collection, **doc}, json_options=JSON_OPTS))
                fh.write("\n")
                touched.add(fh)
                self.written[collection] = self.written.get(collection, 0) + 1
            for fh in touched:
                fh.flush()

    def close(self) -> None:
        for fh in self._files.values():
            try:
                fh.close()
            except OSError:
                pass
        self._files = {}

    # ------------------------------------------------------- maintenance
    def day_dirs(self) -> list[tuple[date, Path]]:
        out = []
        for p in self.root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
            try:
                out.append((date(int(p.parent.parent.name), int(p.parent.name), int(p.name)), p))
            except ValueError:
                continue
        return sorted(out)

    def housekeeping(self, today: date | None = None) -> dict:
        """Compress finished days (UTC) and delete days beyond ``keep_days`` (0 = keep forever)."""
        today = today or datetime.now(timezone.utc).date()
        done = {"compressed": 0, "removed_days": 0}
        for day, path in self.day_dirs():
            if self.keep_days and day < today - timedelta(days=self.keep_days):
                with self._lock:
                    self.close()
                shutil.rmtree(path, ignore_errors=True)
                done["removed_days"] += 1
                continue
            if day >= today - timedelta(days=1):  # keep today + yesterday plain (late records)
                continue
            for f in path.glob("*.jsonl"):
                with self._lock:
                    fh = self._files.pop(f, None)
                    if fh:
                        fh.close()
                    gz = f.with_suffix(".jsonl.gz")
                    with open(f, "rb") as src, gzip.open(gz, "ab", compresslevel=6) as dst:
                        shutil.copyfileobj(src, dst)
                    os.remove(f)
                done["compressed"] += 1
        if done["compressed"] or done["removed_days"]:
            log.info("archive housekeeping", extra=done)
        return done

    def summary(self, days: int = 7) -> list[dict]:
        """Per-day size and file list of the newest ``days`` days (for the app)."""
        out = []
        for day, path in reversed(self.day_dirs()[-days:]):
            files = sorted(path.iterdir())
            out.append({"day": day.isoformat(), "bytes": sum(f.stat().st_size for f in files if f.is_file()),
                        "files": [f.name for f in files]})
        return out
