"""Timestamp-first document ids: every record is saved under its own UTC time.

    2026-11-23T15:24:27.412Z|kali-raspberrypi|DCF49|0
    └── UTC time (ms) ─────┘ └─ station ─────┘ └ source-specific parts

Lexicographic order of these ids == chronological order, so the default ``_id``
index of every collection is a time index, an export sorted by ``_id`` is a
timeline, and ids stay deterministic (re-uploading never duplicates).
"""
from __future__ import annotations

from datetime import datetime, timezone


def ts_key(ts: datetime) -> str:
    """UTC ISO-8601 with milliseconds (BSON dates are ms precision) and a 'Z'."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    u = ts.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def make_id(ts: datetime, *parts) -> str:
    return "|".join([ts_key(ts), *(str(p) for p in parts)])
