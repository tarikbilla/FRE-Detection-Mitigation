"""Relay monitor: reads the ESP32-S3 that watches the FRE's K1..K4 dry contacts.

Integrated from FRE-Relay-Monitor (Rojda Özevli, logger/relay_logger.py,
firmware ``click_logger_multi`` v2). Wire protocol and daily files are kept
**identical**, so the relay dataset continues seamlessly:

    ESP32 lines (115200 baud):  BOOT,<ms>,<fw> | EVT,<ms>,<relay>,<a|b> | STATE,<ms>,K1=<a|b>,..
    Daily files:  <relay_data_dir>/relays_YYYY-MM-DD.jsonl + .csv  (UTC days, schema_version 1)

In addition every record is queued in the platform's durable SQLite buffer as a
``relay_events`` document with a deterministic, timestamp-first ``_id``
(re-uploads never duplicate). Heartbeats (``state``)
are uploaded once per ``relay_state_upload_s``; events, boots and logger records
always. Receive-only: nothing is ever sent to the ESP32 or the FRE.
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..timeid import make_id

log = logging.getLogger(__name__)

FILE_SCHEMA_VERSION = 1   # layout of the daily JSONL/CSV records (unchanged from relay_logger.py)
DOC_SCHEMA_VERSION = 1    # layout of relay_events documents (unchanged from upload_to_mongo.py)
LEVEL_BY_RELAY = {"K1": 100, "K2": 60, "K3": 30, "K4": 0}  # Syna EK 393 sheet
RELAYS = ("K1", "K2", "K3", "K4")
CSV_FIELDS = ["ts_utc", "host", "schema_version", "clock_synced", "type", "esp_ms", "fw",
              "relay", "position", "level_pct", "K1", "K2", "K3", "K4", "detail", "raw"]
PORT_GLOBS = ("/dev/serial/by-id/*CP210*", "/dev/serial/by-id/*Silicon_Labs*")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clock_synced() -> bool | None:
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
        v = out.stdout.strip().lower()
        if v in ("yes", "no"):
            return v == "yes"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def parse_esp_line(line: str) -> dict:
    """ESP line -> record fields. Unknown/garbled lines become type 'other'."""
    p = line.split(",")
    try:
        if p[0] == "EVT" and len(p) == 4 and p[3] in ("a", "b"):
            return {"type": "event", "esp_ms": int(p[1]), "relay": p[2],
                    "position": p[3], "level_pct": LEVEL_BY_RELAY.get(p[2])}
        if p[0] == "STATE" and len(p) >= 3:
            rec: dict = {"type": "state", "esp_ms": int(p[1])}
            for kv in p[2:]:
                k, v = kv.split("=", 1)
                rec[k] = v
            return rec
        if p[0] == "BOOT" and len(p) in (2, 3):
            rec = {"type": "boot", "esp_ms": int(p[1])}
            if len(p) == 3:
                rec["fw"] = p[2]
            return rec
    except (ValueError, IndexError):
        pass
    return {"type": "other"}


def power_level(positions: dict) -> int | None:
    """Requested feed-in level from all four relays (one at 'a' = that level; all 'b' = 100 %)."""
    active = [r for r in RELAYS if positions.get(r) == "a"]
    if len(active) == 1:
        return LEVEL_BY_RELAY[active[0]]
    if not active and all(positions.get(r) == "b" for r in RELAYS):
        return 100
    return None  # ambiguous (switchover, or this unit's all-'a' rest state)


def relay_doc(rec: dict, cfg: Settings) -> dict:
    """File record -> relay_events document; ``_id`` = UTC timestamp first (see timeid)."""
    ts = datetime.fromisoformat(rec["ts_utc"])
    key = f'{rec.get("host")}|{rec["ts_utc"]}|{rec.get("type")}|{rec.get("raw", "")}'
    doc = {k: v for k, v in rec.items() if k not in ("ts_utc", "schema_version")}
    doc.update({
        "_id": make_id(ts, rec.get("host"), "relay", rec.get("type"), hashlib.sha1(key.encode()).hexdigest()[:10]),
        "schema_version": DOC_SCHEMA_VERSION,
        "dataset_version": cfg.dataset_version,
        "experiment_id": cfg.relay_experiment_id,
        "station_id": cfg.station_id,
        "ts": ts,
        "source": "relay-monitor",
    })
    return doc


class DailyWriter:
    """Appends records to relays_<UTC day>.<fmt>, one file per day (same as relay_logger.py)."""

    def __init__(self, outdir: Path, formats=("jsonl", "csv")):
        self.outdir = Path(outdir)
        self.formats = formats
        self.day: str | None = None
        self.files: dict = {}

    def _open(self, day: str) -> None:
        self.close()
        self.outdir.mkdir(parents=True, exist_ok=True)
        for fmt in self.formats:
            path = self.outdir / f"relays_{day}.{fmt}"
            is_new = not path.exists() or path.stat().st_size == 0
            fh = open(path, "a", newline="")
            writer = None
            if fmt == "csv":
                writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if is_new:
                    writer.writeheader()
            self.files[fmt] = (fh, writer)
        self.day = day

    def write(self, rec: dict) -> None:
        day = rec["ts_utc"][:10]
        if day != self.day:
            self._open(day)
        for fmt, (fh, writer) in self.files.items():
            if fmt == "csv":
                writer.writerow(rec)
            else:
                fh.write(json.dumps(rec) + "\n")
            fh.flush()

    def files_missing(self) -> bool:
        for fmt, (fh, _) in self.files.items():
            try:
                expected = self.outdir / f"relays_{self.day}.{fmt}"
                if os.fstat(fh.fileno()).st_nlink == 0 or not expected.exists():
                    return True
            except OSError:
                return True
        return False

    def close(self) -> None:
        for fh, _ in self.files.values():
            try:
                fh.close()
            except OSError:
                pass
        self.files = {}
        self.day = None


def resolve_port(setting: str, globs=PORT_GLOBS) -> str | None:
    if setting and setting != "auto":
        return setting
    for pattern in globs:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class RelayMonitor(threading.Thread):
    """Long-running reader for the ESP32 relay monitor. Never raises out of ``run``."""

    def __init__(self, cfg: Settings, buffer=None, uploader=None):
        super().__init__(name="relay-monitor", daemon=True)
        self.cfg = cfg
        self.buffer = buffer
        self.uploader = uploader
        self.host = socket.gethostname()
        self.writer = DailyWriter(cfg.relay_data_dir)
        self.live_path = Path(cfg.data_dir) / "relay_live.json"
        self._stop_evt = threading.Event()
        self.synced = clock_synced()
        self._last_clock = time.monotonic()
        self._last_disk = 0.0
        self._last_file_check = time.monotonic()
        self._last_state_upload = float("-inf")
        self._last_live = 0.0
        self.port: str | None = None
        self.state = {
            "connected": False, "port": None, "fw": None, "positions": {r: None for r in RELAYS},
            "level_pct": None, "last_line_ts": None, "last_event_ts": None, "events_today": 0,
            "events_total": 0, "lines": 0, "boots": 0, "in_gap": False, "error": None,
            "recent": [], "started_at": utcnow().isoformat(timespec="seconds"),
        }
        self._recent: deque = deque(maxlen=12)
        self._today = utcnow().date().isoformat()

    # ------------------------------------------------------------- records
    def record(self, **fields) -> dict:
        rec = {"ts_utc": utcnow().isoformat(timespec="milliseconds"), "host": self.host,
               "schema_version": FILE_SCHEMA_VERSION, "clock_synced": self.synced}
        rec.update(fields)
        for _ in range(3):
            try:
                self.writer.write(rec)
                break
            except OSError as exc:
                log.warning("relay file write failed; retrying", extra={"error": str(exc)})
                self.writer.close()
                if self._stop_evt.wait(5):
                    break
        self._enqueue(rec)
        return rec

    def _enqueue(self, rec: dict) -> None:
        if self.buffer is None:
            return
        if rec.get("type") == "state":
            now = time.monotonic()
            if now - self._last_state_upload < self.cfg.relay_state_upload_s:
                return
            self._last_state_upload = now
        try:
            self.buffer.put("relay_events", relay_doc(rec, self.cfg), "insert")
            if rec.get("type") == "event" and self.uploader is not None:
                self.uploader.trigger()  # relay switches are rare and important: upload promptly
        except Exception:  # never lose the file record because of the buffer
            log.exception("could not buffer relay record")

    # ---------------------------------------------------------------- live
    def _apply(self, rec: dict) -> None:
        st = self.state
        typ = rec.get("type")
        st["last_line_ts"] = rec["ts_utc"]
        st["lines"] += 1
        if typ == "boot":
            st["boots"] += 1
            st["fw"] = rec.get("fw", st["fw"])
        elif typ == "state":
            for r in RELAYS:
                if rec.get(r) in ("a", "b"):
                    st["positions"][r] = rec[r]
        elif typ == "event":
            st["positions"][rec["relay"]] = rec["position"]
            st["last_event_ts"] = rec["ts_utc"]
            day = rec["ts_utc"][:10]
            if day != self._today:
                self._today, st["events_today"] = day, 0
            st["events_today"] += 1
            st["events_total"] += 1
        if typ in ("event", "boot", "serial_connected", "serial_lost", "gap", "data_resumed", "disk_low",
                   "file_reopened"):
            self._recent.appendleft({"ts": rec["ts_utc"], "type": typ, "relay": rec.get("relay"),
                                     "position": rec.get("position"), "level_pct": rec.get("level_pct"),
                                     "esp_ms": rec.get("esp_ms"), "detail": rec.get("detail")})
        st["level_pct"] = power_level(st["positions"])

    def write_live(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_live < 1.0:
            return
        self._last_live = now
        self.state["recent"] = list(self._recent)
        self.state["updated_at"] = utcnow().isoformat(timespec="milliseconds")
        try:
            tmp = self.live_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, default=str))
            tmp.replace(self.live_path)
        except OSError:
            log.debug("could not write relay_live.json", exc_info=True)

    # -------------------------------------------------------- housekeeping
    def housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_file_check > 5:
            self._last_file_check = now
            if self.writer.files and self.writer.files_missing():
                log.warning("relay data file was deleted or moved; starting a new one")
                self.writer.close()
                self.record(type="file_reopened", detail="previous file was deleted or moved while open")
        if now - self._last_clock > 60:
            self._last_clock = now
            self.synced = clock_synced()
        if now - self._last_disk > 600:
            self._last_disk = now
            try:
                free_mb = shutil.disk_usage(self.cfg.relay_data_dir).free // (1024 * 1024)
                if free_mb < 500:
                    self.record(type="disk_low", detail=f"{free_mb} MB free")
                    log.warning("disk low", extra={"free_mb": free_mb})
            except OSError:
                pass

    def seed_from_files(self) -> None:
        """Restore last known positions/firmware from the newest daily file (display only)."""
        files = sorted(Path(self.cfg.relay_data_dir).glob("relays_*.jsonl"))
        if not files:
            return
        try:  # one UTC day is ~0.3 MB: read it whole so "events today" is exact
            lines = files[-1].read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "state":
                for r in RELAYS:
                    if rec.get(r) in ("a", "b"):
                        self.state["positions"][r] = rec[r]
            elif rec.get("type") == "event":
                self.state["positions"][rec.get("relay")] = rec.get("position")
                self.state["last_event_ts"] = rec["ts_utc"]
                if rec["ts_utc"][:10] == self._today:
                    self.state["events_today"] += 1
                self._recent.appendleft({"ts": rec["ts_utc"], "type": "event", "relay": rec.get("relay"),
                                         "position": rec.get("position"), "level_pct": rec.get("level_pct"),
                                         "esp_ms": rec.get("esp_ms"), "detail": None})
            elif rec.get("type") == "boot":
                self.state["fw"] = rec.get("fw", self.state["fw"])
        self.state["level_pct"] = power_level(self.state["positions"])

    # ------------------------------------------------------------- serial
    def _connect(self):
        import serial
        warned = False
        while not self._stop_evt.is_set():
            port = resolve_port(self.cfg.relay_port)
            try:
                if not port:
                    raise OSError("no ESP32 serial port found (/dev/serial/by-id/*CP210*)")
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, self.cfg.relay_baud, 1
                ser.dtr = False  # keep DTR/RTS released: don't hold the ESP32 in reset
                ser.rts = False
                ser.exclusive = True  # never share the ESP port with a second reader
                ser.open()
                self.port = port
                self.state.update(connected=True, port=port, error=None)
                self.record(type="serial_connected", detail=port)
                log.info("relay monitor connected", extra={"port": port})
                self.write_live(force=True)
                return ser
            except (OSError, serial.SerialException) as exc:
                self.state.update(connected=False, port=port, error=str(exc)[:200])
                self.write_live(force=True)
                if not warned:
                    self.record(type="serial_unavailable", detail=str(exc)[:300])
                    log.warning("relay monitor cannot open port; retrying every 5 s",
                                extra={"port": port, "error": str(exc)})
                    warned = True
                self._stop_evt.wait(5)
        return None

    def run(self) -> None:
        import serial
        try:
            self.cfg.relay_data_dir.mkdir(parents=True, exist_ok=True)
            Path(self.cfg.data_dir).mkdir(parents=True, exist_ok=True)
            self.seed_from_files()
            self.record(type="logger_start", detail=f"port={self.cfg.relay_port} (fre-platform)")
            ser = self._connect()
            last_rx = time.monotonic()
            while ser is not None and not self._stop_evt.is_set():
                self.housekeeping()
                try:
                    raw = ser.readline()
                except (serial.SerialException, OSError) as exc:
                    self.record(type="serial_lost", detail=str(exc)[:300])
                    self.state.update(connected=False, error=str(exc)[:200])
                    log.warning("relay monitor serial lost; reconnecting", extra={"error": str(exc)})
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._stop_evt.wait(2)
                    ser = self._connect()
                    last_rx, self.state["in_gap"] = time.monotonic(), False
                    continue
                if not raw:
                    silent = time.monotonic() - last_rx
                    if not self.state["in_gap"] and silent > self.cfg.relay_gap_warn_s:
                        self.record(type="gap", detail=f"no data from ESP for {int(silent)} s")
                        log.warning("relay monitor: no data from ESP", extra={"silent_s": int(silent)})
                        self.state["in_gap"] = True
                    self.write_live()
                    continue
                last_rx = time.monotonic()
                if self.state["in_gap"]:
                    rec = self.record(type="data_resumed")
                    self._apply(rec)
                    self.state["in_gap"] = False
                line = "".join(ch for ch in raw.decode("ascii", errors="ignore") if ch.isprintable()).strip()
                if not line:
                    continue
                rec = self.record(**parse_esp_line(line), raw=line)
                self._apply(rec)
                if rec["type"] == "event":
                    log.info("relay switched", extra={"relay": rec["relay"], "position": rec["position"],
                                                      "level_pct": rec["level_pct"], "esp_ms": rec["esp_ms"]})
                self.write_live(force=rec["type"] in ("event", "boot"))
            if ser is not None:
                ser.close()
        except Exception:  # keep the rest of the platform alive
            log.exception("relay monitor crashed")
            self.state.update(connected=False, error="crashed - see log")
        finally:
            self.writer.close()
            self.state["connected"] = False
            self.write_live(force=True)

    def stop(self) -> None:
        self._stop_evt.set()
