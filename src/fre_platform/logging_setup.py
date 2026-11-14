"""Structured logging with automatic secret redaction.

Every record is passed through :func:`fre_platform.config.redact_text`
before it is emitted, so a MongoDB connection string that sneaks into an
exception message is still never written to the journal or log files.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import redact_text

_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``extra={...}`` fields are included."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact_text(json.dumps(payload, default=str))


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def setup_logging(level: str = "INFO", fmt: str = "json", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())

    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    else:
        formatter = RedactingTextFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "fre-platform.log", maxBytes=10 * 1024 * 1024,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    # pymongo is chatty at DEBUG; keep it at WARNING unless explicitly debugging.
    logging.getLogger("pymongo").setLevel(logging.WARNING if level.upper() != "DEBUG" else logging.INFO)
