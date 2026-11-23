"""Integrated platform: every data source of the FRE testbed in one supervised process.

    SDR collector (Airspy HF+)  ─┐
    Relay monitor (ESP32-S3)    ─┼─► one durable SQLite buffer ─► one uploader ─► MongoDB (FRE_MONGODB_DB)
    IR probe (IEC 62056-21)     ─┘
    Backup scheduler   daily dump of all configured databases to <backup_dir>
    Stats publisher    MongoDB counts per collection -> mongo_stats.json (dashboard)

Each subsystem runs in its own thread. A crash in one (receiver unplugged, ESP
gone, IR head busy) is logged and that subsystem restarts on its own; the others
keep collecting. State for the dashboard is published as small JSON files in
``data_dir`` (live.json, status.json, relay_live.json, ir_live.json,
mongo_stats.json, platform.json), so the dashboard never needs database access.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings, redact_text

log = logging.getLogger(__name__)

SYSTEM_DBS = ("admin", "local", "config")


def write_json(path: Path, obj: dict) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, default=str))
        tmp.replace(path)
    except OSError:
        log.debug("could not write %s", path, exc_info=True)


class StatsPublisher(threading.Thread):
    """Publishes document counts / sizes of the active and legacy databases."""

    def __init__(self, cfg: Settings, store_factory):
        super().__init__(name="stats", daemon=True)
        self.cfg = cfg
        self.store_factory = store_factory
        self.path = Path(cfg.data_dir) / "mongo_stats.json"
        self._stop_evt = threading.Event()
        self._store = None
        self.last: dict = {}

    def sample(self) -> dict:
        if self._store is None:
            self._store = self.store_factory()
        client = self._store.client
        t = time.perf_counter()
        client.admin.command("ping")
        latency = round((time.perf_counter() - t) * 1000, 1)
        try:
            names = [d for d in client.list_database_names() if d not in SYSTEM_DBS]
        except Exception:  # noqa: BLE001 - user without listDatabases
            names = list(dict.fromkeys([self.cfg.mongodb_db, *self.cfg.backup_db_list]))
        dbs: dict = {}
        used = 0.0
        for name in names:
            db = client[name]
            from .backup import real_collections
            colls = {c: db[c].estimated_document_count() for c in sorted(real_collections(db))
                     if not c.startswith(("system.", "_healthcheck_"))}
            try:
                s = db.command("dbStats")
                data_mb, index_mb = s.get("dataSize", 0) / 2**20, s.get("indexSize", 0) / 2**20
            except Exception:  # noqa: BLE001
                data_mb = index_mb = 0.0
            used += data_mb + index_mb
            dbs[name] = {"collections": colls, "documents": sum(colls.values()),
                         "data_mb": round(data_mb, 2), "index_mb": round(index_mb, 2),
                         "active": name == self.cfg.mongodb_db}
        return {"ok": True, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "latency_ms": latency, "active_db": self.cfg.mongodb_db, "databases": dbs,
                "cluster_used_mb": round(used, 2), "quota_mb": self.cfg.atlas_quota_mb,
                "soft_limit_pct": self.cfg.atlas_soft_limit_pct}

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.last = self.sample()
            except Exception as exc:  # noqa: BLE001
                self.last = {**self.last, "ok": False,
                             "error": redact_text(f"{type(exc).__name__}: {exc}")[:300],
                             "error_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                if self._store is not None:
                    try:
                        self._store.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._store = None
            write_json(self.path, self.last)
            self._stop_evt.wait(self.cfg.stats_interval_s)
        if self._store is not None:
            self._store.close()

    def stop(self) -> None:
        self._stop_evt.set()


class SdrSupervisor(threading.Thread):
    """Runs the SDR collector and restarts it (fresh source) after a failure."""

    def __init__(self, cfg: Settings, buffer, uploader, models, stop: threading.Event, on_block=None,
                 max_blocks: int | None = None):
        super().__init__(name="sdr", daemon=True)
        self.max_blocks = max_blocks
        self.cfg, self.buffer, self.uploader, self.models = cfg, buffer, uploader, models
        self.stop_evt = stop
        self.on_block = on_block
        self.collector = None
        self.state = {"running": False, "restarts": 0, "last_error": None, "finished": False}

    def run(self) -> None:
        from .pipeline import Collector
        from .sources import create_source
        failures = 0
        while not self.stop_evt.is_set():
            try:
                source = create_source(self.cfg)
                self.collector = Collector(self.cfg, source, self.buffer, self.uploader, self.models,
                                           on_block=self.on_block, owns_uploader=False)
                self.state["running"] = True
                self.collector.run(self.stop_evt, max_blocks=self.max_blocks)
                if not self.stop_evt.is_set():  # replay/simulation exhausted or max_blocks reached
                    self.state["finished"] = True
                    if self.max_blocks is not None:
                        self.stop_evt.set()  # bounded run (tests): stop the whole platform
                    break
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.state.update(running=False, restarts=self.state["restarts"] + 1,
                                  last_error=redact_text(f"{type(exc).__name__}: {exc}")[:300])
                wait = min(300, 15 * failures)
                log.exception("SDR collector failed; restarting in %d s", wait)
                self.stop_evt.wait(wait)
                continue
            failures = 0
        self.state["running"] = False


class Platform:
    """Starts, supervises and stops all subsystems."""

    def __init__(self, cfg: Settings, models=None, upload: bool = True, sdr: bool = True, on_block=None,
                 max_blocks: int | None = None):
        self.cfg = cfg
        self.max_blocks = max_blocks
        self.models = models
        self.upload = upload
        self.enable_sdr = sdr
        self.on_block = on_block
        self.stop_evt = threading.Event()
        self.started_at = datetime.now(timezone.utc)
        self.buffer = self.uploader = self.sdr = self.relay = self.ir = self.backup = self.stats = None
        self.platform_path = Path(cfg.data_dir) / "platform.json"
        self.telegrams_path = Path(cfg.data_dir) / "telegrams_recent.json"
        self.recent_telegrams: deque = deque(maxlen=500)
        self._manual_backup: threading.Thread | None = None
        self._last_health = 0.0
        self.archive = None
        self._last_archive_hk = 0.0

    def _store_factory(self):
        from .storage.mongo import MongoStore
        return lambda: MongoStore.from_config(self.cfg)

    def start(self) -> None:
        from .storage.buffer import LocalBuffer
        from .storage.uploader import Uploader
        cfg = self.cfg
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        try:  # keep the telegram history across restarts (app Telegrams tab)
            old = json.loads(self.telegrams_path.read_text()).get("telegrams", [])
            self.recent_telegrams.extend(old[: self.recent_telegrams.maxlen])
        except (OSError, ValueError, AttributeError):
            pass
        self.buffer = LocalBuffer(Path(cfg.data_dir) / "buffer.sqlite3", cfg.max_buffer_rows)
        if cfg.archive_enabled:
            from .archive import TimeArchive
            self.archive = TimeArchive(cfg.archive_path, cfg.archive_keep_days)
            self.buffer.archive = self.archive
        if self.upload and cfg.mongodb_configured:
            self.uploader = Uploader(self.buffer, self._store_factory(), cfg)
            self.uploader.start()
            self.stats = StatsPublisher(cfg, self._store_factory())
            self.stats.start()
            if cfg.backup_enabled:
                from .backup import BackupScheduler
                self.backup = BackupScheduler(cfg)
                self.backup.start()
        else:
            log.warning("MongoDB upload disabled or not configured: data stays in the local buffer")
        if cfg.relay_enabled:
            from .relay import RelayMonitor
            self.relay = RelayMonitor(cfg, self.buffer, self.uploader)
            self.relay.start()
        if cfg.ir_enabled:
            from .ir import IrProbe
            self.ir = IrProbe(cfg, self.buffer)
            self.ir.start()
        if self.enable_sdr:
            self.sdr = SdrSupervisor(cfg, self.buffer, self.uploader, self.models, self.stop_evt, self._on_block,
                                     self.max_blocks)
            self.sdr.start()
        log.info("platform started", extra={"version": __version__, "db": cfg.mongodb_db,
                                            "relay": cfg.relay_enabled, "ir": cfg.ir_enabled,
                                            "backup": cfg.backup_enabled, "sdr": self.enable_sdr})

    # --------------------------------------------------------- telegrams
    def _on_block(self, summary: dict) -> None:
        """Keep a rolling list of received telegrams for the app's Telegrams tab."""
        new = False
        for c in summary.get("channels", []):
            for tg in c.get("telegrams", []):
                self.recent_telegrams.appendleft({"ts": summary["ts"], "seq": summary["seq"], "channel": c["name"],
                                                  "snr_db": c.get("snr_db"), **tg})
                new = True
        if new:
            write_json(self.telegrams_path, {"updated_at": summary["ts"], "telegrams": list(self.recent_telegrams)})
        if self.on_block is not None:
            self.on_block(summary)

    # ---------------------------------------------------------- commands
    def process_commands(self) -> None:
        from . import control
        for req in control.pending(self.cfg.data_dir):
            action = req.get("action")
            try:
                if action == "flush":
                    if self.uploader is None:
                        control.respond(self.cfg.data_dir, req, "error", message="uploads are disabled")
                    else:
                        rows = self.buffer.count()
                        self.uploader.trigger()
                        control.respond(self.cfg.data_dir, req, "done",
                                        message=f"upload triggered ({rows} buffered rows)")
                elif action == "ir_probe":
                    self._cmd_ir_probe(req)
                elif action == "backup":
                    self._cmd_backup(req)
                else:
                    control.respond(self.cfg.data_dir, req, "error", message=f"unknown action {action!r}")
            except Exception as exc:  # noqa: BLE001 - a bad request must never stop the platform
                log.exception("command failed", extra={"action": action})
                control.respond(self.cfg.data_dir, req, "error", message=redact_text(str(exc))[:300])
        control.prune_done(self.cfg.data_dir)

    def _cmd_ir_probe(self, req: dict) -> None:
        from . import control
        if self.ir is None:
            control.respond(self.cfg.data_dir, req, "error", message="IR probe is disabled (FRE_IR_ENABLED=false)")
            return
        if not self.ir.state.get("head_present"):
            control.respond(self.cfg.data_dir, req, "error", message="IR head not connected")
            return
        n = self.ir.probe_now()
        control.respond(self.cfg.data_dir, req, "running", message="probing ...")

        def wait():
            deadline = time.monotonic() + 30
            while self.ir.state["probes"] <= n and time.monotonic() < deadline:
                time.sleep(0.2)
            res = self.ir.last_result or {}
            if self.ir.state["probes"] <= n:
                control.respond(self.cfg.data_dir, req, "error", message="probe did not finish within 30 s")
            elif res.get("responded"):
                ident = (res.get("ident") or {}).get("line")
                control.respond(self.cfg.data_dir, req, "done",
                                message=f"FRE ANSWERED: {ident or res.get('rx_hex', '')[:40]}", result=res)
            else:
                msg = res.get("error") or ("no answer (head echo ok)" if res.get("echo") else "no answer, no echo")
                control.respond(self.cfg.data_dir, req, "done", message=msg, result=res)
        threading.Thread(target=wait, name="cmd-ir", daemon=True).start()

    def _cmd_backup(self, req: dict) -> None:
        from . import control
        from .backup import run_backup
        if not self.cfg.mongodb_configured or self.uploader is None:
            control.respond(self.cfg.data_dir, req, "error", message="MongoDB is not configured / uploads disabled")
            return
        busy = (self._manual_backup and self._manual_backup.is_alive()) or \
            (self.backup is not None and self.backup.state.get("running"))
        if busy:
            control.respond(self.cfg.data_dir, req, "error", message="a backup is already running")
            return
        control.respond(self.cfg.data_dir, req, "running", message="backup running ...")

        def job():
            if self.backup is not None:
                self.backup.state["running"] = True
            try:
                m = run_backup(self.cfg)
                msg = f"backup {m['name']}: {m['documents']:,} docs, {m['bytes'] / 2**20:.1f} MB in {m['duration_s']:.0f} s"
                if self.backup is not None:
                    self.backup.state.update(last_result={k: m[k] for k in ("name", "documents", "bytes",
                                                                          "duration_s")}, last_error=None)
                control.respond(self.cfg.data_dir, req, "done", message=msg)
            except Exception as exc:  # noqa: BLE001
                err = redact_text(f"{type(exc).__name__}: {exc}")[:300]
                if self.backup is not None:
                    self.backup.state["last_error"] = err
                control.respond(self.cfg.data_dir, req, "error", message=err)
            finally:
                if self.backup is not None:
                    self.backup.state["running"] = False
        self._manual_backup = threading.Thread(target=job, name="cmd-backup", daemon=True)
        self._manual_backup.start()

    def health(self) -> dict:
        def alive(t):
            return bool(t and t.is_alive())
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid(),
            "version": __version__, "started_at": self.started_at.isoformat(timespec="seconds"),
            "mongodb_db": self.cfg.mongodb_db, "station_id": self.cfg.station_id,
            "subsystems": {
                "sdr": {"enabled": self.enable_sdr, "alive": alive(self.sdr), **(self.sdr.state if self.sdr else {})},
                "relay": {"enabled": self.cfg.relay_enabled, "alive": alive(self.relay)},
                "ir": {"enabled": self.cfg.ir_enabled, "alive": alive(self.ir)},
                "uploader": {"enabled": self.uploader is not None, "alive": alive(self.uploader),
                             **(dict(self.uploader.stats) if self.uploader else {})},
                "stats": {"alive": alive(self.stats)},
                "backup": {"enabled": self.backup is not None, "alive": alive(self.backup),
                           **(self.backup.state if self.backup else {})},
            },
            "buffer": {"rows": self.buffer.count(), "by_collection": self.buffer.count_by_collection(),
                       "dead_letter": self.buffer.dead_letter_count()} if self.buffer else None,
            "archive": {"path": str(self.cfg.archive_path), "written": dict(self.archive.written)}
            if self.archive else None,
        }

    def run_forever(self) -> None:
        """Block until :attr:`stop_evt` is set, publishing health every few seconds."""
        while not self.stop_evt.is_set():
            if time.monotonic() - self._last_health >= 2:
                self._last_health = time.monotonic()
                write_json(self.platform_path, self.health())
            try:
                self.process_commands()
            except Exception:  # noqa: BLE001
                log.exception("command processing failed")
            if self.archive is not None and time.monotonic() - self._last_archive_hk > 600:
                self._last_archive_hk = time.monotonic()
                threading.Thread(target=self._archive_housekeeping, name="archive-hk", daemon=True).start()
            # restart a crashed relay/IR thread (they catch everything, so this is a last resort)
            if self.relay is not None and not self.relay.is_alive() and not self.stop_evt.is_set():
                from .relay import RelayMonitor
                log.error("relay monitor thread died; restarting")
                self.relay = RelayMonitor(self.cfg, self.buffer, self.uploader)
                self.relay.start()
            if self.ir is not None and not self.ir.is_alive() and not self.stop_evt.is_set():
                from .ir import IrProbe
                log.error("IR probe thread died; restarting")
                self.ir = IrProbe(self.cfg, self.buffer)
                self.ir.start()
            self.stop_evt.wait(1)

    def _archive_housekeeping(self) -> None:
        try:
            self.archive.housekeeping()
        except Exception:  # noqa: BLE001
            log.exception("archive housekeeping failed")

    def shutdown(self) -> None:
        self.stop_evt.set()
        for t in (self.relay, self.ir, self.backup, self.stats):
            if t is not None:
                t.stop()
        if self.sdr is not None:
            self.sdr.join(timeout=60)
        for t in (self.relay, self.ir):
            if t is not None:
                t.join(timeout=10)
        if self.uploader is not None:
            self.uploader.stop()  # final flush of everything still buffered
        write_json(self.platform_path, self.health())
        if self.buffer is not None:
            self.buffer.close()
        if self.archive is not None:
            self.archive.close()
        log.info("platform stopped")
