"""Command-line interface: ``fre-platform <command>`` / ``python -m fre_platform``."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .config import ConfigError, Settings, redact_text
from .logging_setup import setup_logging

log = logging.getLogger("fre_platform.cli")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _settings(args, **overrides) -> Settings:
    return Settings.from_env(env_file=getattr(args, "env_file", None), **overrides)


def _buffer(cfg: Settings):
    from .storage.buffer import LocalBuffer
    return LocalBuffer(Path(cfg.data_dir) / "buffer.sqlite3", cfg.max_buffer_rows)


def _models(cfg: Settings):
    from .detection.ml import FreModels
    if Path(cfg.model_path).is_file():
        try:
            m = FreModels.load(cfg.model_path)
            log.info("loaded ML models", extra={"path": str(cfg.model_path), "trained_at": m.metadata.get("trained_at")})
            return m
        except Exception:
            log.exception("could not load ML models; continuing with rule-based detection only")
    else:
        log.info("no ML model file; rule-based detection only", extra={"path": str(cfg.model_path)})
    return None


def _uploader(cfg: Settings, buffer, enabled: bool = True):
    from .storage.mongo import MongoStore
    from .storage.uploader import Uploader
    if not enabled:
        return None
    if not cfg.mongodb_configured:
        log.warning("FRE_MONGODB_URI not set: data is kept in the local buffer until it is configured")
        return None
    return Uploader(buffer, lambda: MongoStore.from_config(cfg), cfg)


_COLORS = {"normal": "\033[32m", "suspicious": "\033[33m", "anomalous": "\033[31;1m"}
_RESET = "\033[0m"


def _num(v, fmt: str) -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "-"


def format_live(s: dict, color: bool = False) -> str:
    """Render one live block summary as console lines (one per channel)."""
    t = s["ts"][11:23]
    head = (f"{t}  #{s['seq']:<6} band floor {_num(s['band_floor_db'], '7.1f')} dBFS/Hz  "
            f"power {_num(s['band_power_dbfs'], '6.1f')} dBFS")
    if not s["valid"]:
        head += "  INVALID: " + "; ".join(s["reasons"])
    if s["band_rules"]:
        head += "  band: " + ",".join(s["band_rules"])
    up = s.get("upload")
    head += f"  buffer {s['buffer_rows']}" + ("" if up is None else
                                              f"  atlas {'online' if up['online'] else 'offline'} ({up['uploaded']} up)")
    lines = [head]
    for c in s["channels"]:
        v = c["verdict"]
        verdict = f"{_COLORS[v]}{v:<10}{_RESET}" if color else f"{v:<10}"
        extra = ",".join(c["rules"]) + (f" ml={c['ml']}" if c.get("ml") else "")
        lines.append(f"    {c['name']:<6} {c['freq_hz'] / 1e3:7.1f} kHz  P {_num(c['power_dbfs'], '6.1f')} dBFS  "
                     f"SNR {_num(c['snr_db'], '6.1f')} dB  off {_num(c['offset_hz'], '+6.1f')} Hz  "
                     f"FSK {_num(c['fsk_activity'], '4.2f')}  lines {c['lines']:<2}  {verdict} "
                     f"{c['signal_state']:<7} {extra}".rstrip())
    return "\n".join(lines)


def _printer():
    color = sys.stdout.isatty()

    def show(summary: dict) -> None:
        print(format_live(summary, color), flush=True)
    return show


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, _frame):
        log.info("signal received, stopping", extra={"signal": signum})
        stop.set()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------- commands
def cmd_collect(args) -> int:
    from .pipeline import Collector
    from .sources import create_source
    overrides = {"source": args.source} if args.source else {}
    cfg = _settings(args, **overrides)
    buffer = _buffer(cfg)
    source = create_source(cfg)
    stop = threading.Event()
    _install_signal_handlers(stop)
    collector = Collector(cfg, source, buffer, _uploader(cfg, buffer, not args.no_upload), _models(cfg),
                          on_block=_printer() if args.live else None)
    try:
        # runs until Ctrl+C / SIGTERM unless --max-blocks is given
        stats = collector.run(stop, max_blocks=args.max_blocks)
    finally:
        buffer.close()
    log.info("collection finished", extra={"stats": stats})
    return 0


def cmd_watch(args) -> int:
    """Follow the live output of the running collector (e.g. the systemd service)."""
    import time
    cfg = _settings(args)
    path = Path(cfg.data_dir) / "live.json"
    show = _printer()
    last_seq, last_change = None, time.monotonic()
    print(f"watching {path} (Ctrl+C to stop)", flush=True)
    try:
        while True:
            try:
                s = json.loads(path.read_text())
            except (OSError, ValueError):
                s = None
            if s and (s["seq"], s["ts"]) != last_seq:
                last_seq, last_change = (s["seq"], s["ts"]), time.monotonic()
                show(s)
            elif time.monotonic() - last_change > max(10.0, 5 * cfg.block_seconds):
                print(f"... no new data for {time.monotonic() - last_change:.0f} s "
                      f"(is the collector running? systemctl status fre-collector)", flush=True)
                last_change = time.monotonic()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_simulate(args) -> int:
    from .pipeline import Collector
    from .sources.simulated import SimulatedSource
    cfg = _settings(args, source="simulated")
    buffer = _buffer(cfg)
    source = SimulatedSource(cfg, scenario=args.scenario, seed=args.seed, realtime=args.realtime,
                             max_blocks=args.blocks)
    stop = threading.Event()
    _install_signal_handlers(stop)
    collector = Collector(cfg, source, buffer, _uploader(cfg, buffer, not args.no_upload), _models(cfg),
                          keep_all_measurements=True)
    try:
        stats = collector.run(stop, max_blocks=args.blocks)
        _print({"stats": stats, "buffer_rows": buffer.count()})
    finally:
        buffer.close()
    return 0


def cmd_db_check(args) -> int:
    from .storage.mongo import MongoStore
    cfg = _settings(args)
    if not cfg.mongodb_configured:
        print("FRE_MONGODB_URI is not set", file=sys.stderr)
        return 2
    store = MongoStore.from_config(cfg)
    out: dict = {"database": cfg.mongodb_db}
    try:
        out["ping"] = store.ping()
        if not args.no_schema:
            out["schema"] = store.ensure_schema()
        out["roundtrip"] = store.healthcheck_roundtrip(cfg.station_id)
        out["counts"] = store.counts()
        out["ok"] = out["roundtrip"]["insert_read_delete_ok"]
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = redact_text(f"{type(exc).__name__}: {exc}")[:800]
    finally:
        store.close()
    _print(out)
    return 0 if out["ok"] else 1


def cmd_buffer_status(args) -> int:
    cfg = _settings(args)
    buf = _buffer(cfg)
    status_file = Path(cfg.data_dir) / "status.json"
    out = {"rows": buf.count(), "by_collection": buf.count_by_collection(), "dead_letter": buf.dead_letter_count(),
           "oldest_age_s": buf.oldest_age_s(),
           "collector_status": json.loads(status_file.read_text()) if status_file.is_file() else None}
    buf.close()
    _print(out)
    return 0


def cmd_flush(args) -> int:
    from .storage.mongo import MongoStore
    from .storage.uploader import Uploader
    cfg = _settings(args)
    buf = _buffer(cfg)
    if not cfg.mongodb_configured:
        print("FRE_MONGODB_URI is not set", file=sys.stderr)
        return 2
    up = Uploader(buf, lambda: MongoStore.from_config(cfg), cfg)
    before = buf.count()
    ok = True
    while buf.count() and ok:
        ok = up.flush_once(max_batches=1000)
    if up.store:
        up.store.close()
    out = {"ok": ok, "rows_before": before, "rows_after": buf.count(), "uploaded": up.stats["uploaded"],
           "rejected": up.stats["rejected"], "last_error": up.stats["last_error"]}
    buf.close()
    _print(out)
    return 0 if ok else 1


def cmd_train(args) -> int:
    import pandas as pd
    from .detection.ml import generate_simulated_dataset, train_models
    cfg = _settings(args, source="simulated")
    if args.from_db:
        from .storage.mongo import MongoStore
        store = MongoStore.from_config(cfg)
        q = {"label": {"$ne": None}}
        if args.dataset_version:
            q["dataset_version"] = args.dataset_version
        docs = list(store.db.measurements.find(q, {"features": 1, "band": 1, "label": 1}))
        store.close()
        df = pd.DataFrame([{**d["features"], "band_flatness": d.get("band", {}).get("band_flatness", 0.0),
                            "band_floor_rise_db": d.get("band", {}).get("band_floor_rise_db", 0.0),
                            "label": d["label"]} for d in docs])
        source = "mongodb"
        if df.empty:
            print("no labelled measurements found in MongoDB", file=sys.stderr)
            return 1
    else:
        df = generate_simulated_dataset(cfg, blocks_per_scenario=args.blocks_per_scenario, seed=args.seed)
        source = "simulation"
    models, report = train_models(df, cfg.dataset_version, seed=args.seed, source=source)
    models.save(cfg.model_path)
    out = {"model_path": str(cfg.model_path), "rows": int(len(df)), **report}
    if args.report:
        Path(args.report).write_text(json.dumps(out, indent=2, default=str))
    if args.register:
        buf = _buffer(cfg)
        meta = report["metadata"]
        buf.put("models", {"_id": f"{cfg.dataset_version}|{meta['trained_at']}", "schema_version": SCHEMA_VERSION,
                           "station_id": cfg.station_id, "metadata": meta,
                           "classification_report": report["classification_report"],
                           "confusion_matrix": report["confusion_matrix"]}, mode="upsert")
        buf.close()
    _print({k: out[k] for k in ("model_path", "rows", "metadata", "confusion_matrix")})
    return 0


def cmd_mitigation_eval(args) -> int:
    from .mitigation.simulator import run_mitigation_experiment
    cfg = _settings(args, source="simulated")
    table, events = run_mitigation_experiment(cfg, blocks=args.blocks, seed=args.seed, models=_models(cfg))
    print(table.to_string(index=False))
    if args.json:
        Path(args.json).write_text(json.dumps({"strategies": table.to_dict(orient="records"),
                                               "events": int(len(events)),
                                               "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2))
    return 0


def cmd_export(args) -> int:
    import pandas as pd
    from .storage.mongo import MongoStore
    cfg = _settings(args)
    store = MongoStore.from_config(cfg)
    q = {"dataset_version": args.dataset_version} if args.dataset_version else {}
    cur = store.db[args.collection].find(q).sort("ts", 1)
    if args.limit:
        cur = cur.limit(args.limit)
    df = pd.json_normalize(list(cur))
    store.close()
    out = Path(args.out)
    if out.suffix == ".parquet":
        df.to_parquet(out, index=False)
    else:
        df.to_csv(out, index=False)
    print(f"exported {len(df)} rows from {args.collection} to {out}")
    return 0


def cmd_show_config(args) -> int:
    _print(_settings(args).safe_dict())
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fre-platform", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--env-file", help="dotenv file with FRE_* settings (default: $FRE_ENV_FILE or ./.env)")
    p.add_argument("--log-level", help="override FRE_LOG_LEVEL")
    p.add_argument("--log-format", choices=("json", "text"), help="override FRE_LOG_FORMAT")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("collect", help="continuous receive-only collection (service entry point)")
    s.add_argument("--source", choices=("airspyhf", "simulated", "replay"))
    s.add_argument("--max-blocks", type=int)
    s.add_argument("--no-upload", action="store_true", help="buffer locally only")
    s.add_argument("--live", action="store_true", help="print every received block to the console")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("watch", help="show live data from the running collector/service")
    s.add_argument("--interval", type=float, default=0.3, help="poll interval in seconds")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("simulate", help="run a synthetic scenario through the full pipeline")
    s.add_argument("--scenario", default="mixed")
    s.add_argument("--blocks", type=int, default=60)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--realtime", action="store_true")
    s.add_argument("--no-upload", action="store_true")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("db-check", help="test MongoDB connectivity, schema and insert/read/delete")
    s.add_argument("--no-schema", action="store_true", help="skip creating collections/indexes")
    s.set_defaults(func=cmd_db_check)

    sub.add_parser("buffer-status", help="show local buffer and collector status").set_defaults(func=cmd_buffer_status)
    sub.add_parser("flush", help="upload everything in the local buffer now").set_defaults(func=cmd_flush)

    s = sub.add_parser("train", help="train anomaly + classification models")
    s.add_argument("--blocks-per-scenario", type=int, default=40)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--from-db", action="store_true", help="train on labelled measurements in MongoDB")
    s.add_argument("--dataset-version")
    s.add_argument("--register", action="store_true", help="store model metadata in the 'models' collection")
    s.add_argument("--report", help="write the full evaluation report to this JSON file")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("mitigation-eval", help="compare receiver-side mitigation strategies (simulation)")
    s.add_argument("--blocks", type=int, default=200)
    s.add_argument("--seed", type=int, default=11)
    s.add_argument("--json")
    s.set_defaults(func=cmd_mitigation_eval)

    s = sub.add_parser("export", help="export a collection to CSV/Parquet for analysis")
    s.add_argument("--collection", default="measurements")
    s.add_argument("--dataset-version")
    s.add_argument("--limit", type=int)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_export)

    sub.add_parser("show-config", help="print effective settings (secrets redacted)").set_defaults(func=cmd_show_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Settings.from_env(env_file=args.env_file)
    except ConfigError as exc:
        setup_logging("INFO", "text")
        log.error("configuration error: %s", exc)
        return 2
    setup_logging(args.log_level or cfg.log_level, args.log_format or cfg.log_format, cfg.log_dir)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
