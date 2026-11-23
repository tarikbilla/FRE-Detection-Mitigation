"""Command inbox between the app (UI) and the running platform service.

The UI never opens the hardware or runs long jobs itself. It drops a request
file into ``<data_dir>/commands/``; the platform (systemd service or the
foreground run) picks it up within a second, executes it and writes the result
to ``<data_dir>/commands/done/<id>.json``. Works across processes and users of
the ``kali`` group, survives restarts (unprocessed requests are handled after
the next start) and needs no network port.

Actions: ``backup`` (dump MongoDB now), ``ir_probe`` (probe the optical port
now), ``flush`` (upload the local buffer now).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ACTIONS = ("backup", "ir_probe", "flush")
STALE_S = 3600  # requests older than this are discarded unprocessed


def inbox(data_dir) -> Path:
    return Path(data_dir) / "commands"


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, default=str))
    os.replace(tmp, path)


def submit(data_dir, action: str, **params) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")
    cid = f"{time.strftime('%Y%m%d-%H%M%S')}-{action}-{uuid.uuid4().hex[:6]}"
    _write(inbox(data_dir) / f"{cid}.json", {"id": cid, "action": action, "params": params,
                                             "requested_at": datetime.now(timezone.utc).isoformat(),
                                             "pid": os.getpid()})
    return cid


def result(data_dir, cid: str) -> dict | None:
    try:
        return json.loads((inbox(data_dir) / "done" / f"{cid}.json").read_text())
    except (OSError, ValueError):
        return None


def wait(data_dir, cid: str, timeout: float = 120.0, final_only: bool = True) -> dict | None:
    """Poll for the result of ``cid`` (``final_only``: skip 'accepted'/'running' updates)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = result(data_dir, cid)
        if r and (not final_only or r.get("status") not in ("accepted", "running")):
            return r
        time.sleep(0.5)
    return result(data_dir, cid)


def pending(data_dir) -> list[dict]:
    """Unprocessed requests, oldest first (stale ones are removed)."""
    out = []
    d = inbox(data_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            req = json.loads(f.read_text())
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
            continue
        if time.time() - f.stat().st_mtime > STALE_S:
            f.unlink(missing_ok=True)
            continue
        req["_path"] = str(f)
        out.append(req)
    return out


def respond(data_dir, req: dict, status: str, **fields) -> None:
    """Write/overwrite the result for a request; the request file is consumed on first response."""
    _write(inbox(data_dir) / "done" / f"{req['id']}.json",
           {"id": req["id"], "action": req["action"], "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(), **fields})
    Path(req["_path"]).unlink(missing_ok=True)


def prune_done(data_dir, keep_s: float = 86400) -> None:
    d = inbox(data_dir) / "done"
    if d.is_dir():
        for f in d.glob("*.json"):
            if time.time() - f.stat().st_mtime > keep_s:
                f.unlink(missing_ok=True)
