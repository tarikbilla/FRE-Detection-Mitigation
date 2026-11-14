"""Acquisition -> analysis -> buffering pipeline."""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .config import Settings
from .detection.ml import ML_FEATURES, FreModels
from .detection.rules import RuleEngine, RuleHit, verdict_from
from .dsp.features import BAND_FEATURES, CHANNEL_FEATURES, FeatureExtractor, assess_quality
from .mitigation.advisor import recommend
from .sources.base import IQBlock, IQSource, ns_to_datetime
from .storage.buffer import LocalBuffer

log = logging.getLogger(__name__)

NTP_FLAG = Path("/run/systemd/timesync/synchronized")
MAX_CONSECUTIVE_ERRORS = 50


def ntp_synced() -> bool | None:
    if NTP_FLAG.parent.exists():
        return NTP_FLAG.exists()
    return None


class BlockAnalyzer:
    """Stateless-per-call analysis with stateful baselines (no I/O)."""

    def __init__(self, cfg: Settings, models: FreModels | None = None):
        self.cfg = cfg
        self.extractor = FeatureExtractor(cfg)
        self.rules = RuleEngine(cfg)
        self.models = models
        self._prev_ts: int | None = None
        self._ntp: bool | None = ntp_synced()
        self._ntp_checked = time.monotonic()

    def analyze(self, block: IQBlock) -> dict:
        if time.monotonic() - self._ntp_checked > 60:
            self._ntp, self._ntp_checked = ntp_synced(), time.monotonic()
        quality = assess_quality(block, self.cfg.block_samples, self._prev_ts, time.time_ns(), self._ntp)
        self._prev_ts = block.ts_ns
        result = {"seq": block.seq, "ts_ns": block.ts_ns, "quality": quality, "band": {}, "psd": None,
                  "band_hits": [], "channels": []}
        if not quality["valid"]:
            return result

        band, psd = self.extractor.band(block)
        base_floor = self.rules.band_baseline.median() if self.rules.band_baseline.ready else None
        band["band_floor_rise_db"] = round(band["band_noise_floor_db"] - base_floor, 3) if base_floor is not None else 0.0
        band_hits = self.rules.evaluate_band(band, quality)
        result.update(band={k: round(v, 5) for k, v in band.items()}, psd=psd,
                      band_hits=[h.to_dict() for h in band_hits])
        shared = [h for h in band_hits if h.severity != "info"]

        chans = []
        for ch in self.cfg.channels:
            feats = self.extractor.channel(block, ch, band)
            hits: list[RuleHit] = shared + self.rules.evaluate_channel(ch.name, feats, band_hits)
            ml_input = {**feats, "band_flatness": band["band_flatness"],
                        "band_floor_rise_db": band["band_floor_rise_db"]}
            chans.append({"name": ch.name, "freq_hz": ch.freq_hz, "features": feats, "ml_input": ml_input,
                          "hits": hits, "truth": (block.truth or {}).get(ch.name)})

        ml_out = self.models.predict([c["ml_input"] for c in chans]) if self.models else [None] * len(chans)
        for c, ml in zip(chans, ml_out):
            ml_anom = bool(ml and ml.get("is_anomaly"))
            ml_label = ml.get("label") if ml else None
            verdict = verdict_from(c["hits"], ml_anom or (ml_label not in (None, "normal")))
            codes = [h.code for h in c["hits"] if h.severity != "info"]
            c.update(rule_hits=[h.to_dict() for h in c.pop("hits")], ml=ml, verdict=verdict,
                     mitigations=recommend(codes, ml_anom, ml_label) if verdict != "normal" else [])
        result["channels"] = chans
        return result


class Collector:
    def __init__(self, cfg: Settings, source: IQSource, buffer: LocalBuffer, uploader=None,
                 models: FreModels | None = None, keep_all_measurements: bool | None = None):
        self.cfg = cfg
        self.source = source
        self.buffer = buffer
        self.uploader = uploader
        self.analyzer = BlockAnalyzer(cfg, models)
        self.models = models
        self.keep_all = keep_all_measurements
        info = source.device_info()
        serial = info.get("serial") or source.name
        self.device_id = f"{cfg.station_id}:{source.name}:{serial}"
        self.device_info = info
        self.stats = {"blocks": 0, "invalid_blocks": 0, "errors": 0, "timeouts": 0, "docs_buffered": 0,
                      "verdicts": {"normal": 0, "suspicious": 0, "anomalous": 0}, "started_at":
                      datetime.now(timezone.utc).isoformat()}
        self._consecutive_errors = 0
        self._last_heartbeat = time.monotonic()
        self.status_path = Path(cfg.data_dir) / "status.json"
        (Path(cfg.data_dir) / "iq").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ metadata
    def _common(self, block: IQBlock) -> dict:
        return {"schema_version": SCHEMA_VERSION, "dataset_version": self.cfg.dataset_version,
                "experiment_id": self.cfg.experiment_id, "station_id": self.cfg.station_id,
                "device_id": self.device_id, "source": block.source, "ts": block.ts, "ts_ns": block.ts_ns,
                "seq": block.seq, "software_version": __version__}

    def metadata_docs(self) -> list[tuple[str, dict, str]]:
        cfg, now = self.cfg, datetime.now(timezone.utc)
        device = {"_id": self.device_id, "schema_version": SCHEMA_VERSION, "station_id": cfg.station_id,
                  "device_type": self.source.name, "receive_only": True, "info": self.device_info,
                  "host": {"hostname": socket.gethostname(), "platform": platform.platform(),
                           "machine": platform.machine(), "python": platform.python_version()},
                  "software_version": __version__, "updated_at": now}
        experiment = {"_id": cfg.experiment_id, "schema_version": SCHEMA_VERSION, "station_id": cfg.station_id,
                      "description": cfg.experiment_description, "dataset_version": cfg.dataset_version,
                      "device_id": self.device_id, "last_started_at": now,
                      "authorization": {"mode": "receive-only", "transmission": "none",
                                        "note": "passive observation and authorized simulation only"},
                      "conditions": {"antenna": cfg.antenna, "location_label": cfg.location_label,
                                     "source": cfg.source, "center_freq_hz": cfg.center_freq_hz,
                                     "sample_rate_hz": cfg.sample_rate_hz, "block_seconds": cfg.block_seconds,
                                     "channels": [{"name": c.name, "freq_hz": c.freq_hz} for c in cfg.channels],
                                     "airspy": {"agc": cfg.airspy_agc, "attenuation": cfg.airspy_attenuation,
                                                "lna": cfg.airspy_lna},
                                     "scenario": getattr(self.source, "scenario", None)}}
        dataset = {"_id": cfg.dataset_version, "schema_version": SCHEMA_VERSION, "updated_at": now,
                   "description": "FRE receive-only research dataset",
                   "feature_names": list(CHANNEL_FEATURES), "band_feature_names": list(BAND_FEATURES),
                   "ml_feature_names": list(ML_FEATURES),
                   "collections": ["raw_observations", "measurements", "detections", "devices", "experiments"],
                   "sampling": {"raw_every_n_blocks": cfg.raw_every_n_blocks,
                                "measurement_every_n_blocks": cfg.measurement_every_n_blocks,
                                "psd_bins": cfg.psd_bins},
                   "labels": ["normal", "jamming_wideband", "jamming_narrowband", "spoof", "carrier_loss",
                              "freq_offset", "flood"]}
        return [("devices", device, "upsert"), ("experiments", experiment, "upsert"), ("datasets", dataset, "upsert")]

    # ----------------------------------------------------------- documents
    def build_documents(self, block: IQBlock, res: dict) -> list[tuple[str, dict, str]]:
        cfg = self.cfg
        common = self._common(block)
        st = cfg.station_id
        docs: list[tuple[str, dict, str]] = []
        any_alert = any(c["verdict"] != "normal" for c in res["channels"])
        keep_all = self.keep_all if self.keep_all is not None else block.truth is not None
        raw_id = f"{st}|{block.ts_ns}|raw"

        if block.seq % cfg.raw_every_n_blocks == 0 or any_alert or not res["quality"]["valid"] or keep_all:
            raw = {"_id": raw_id, **common, "center_freq_hz": float(block.center_freq_hz),
                   "sample_rate_hz": int(block.sample_rate_hz), "n_samples": int(len(block.samples)),
                   "duration_s": block.duration_s, "quality": res["quality"],
                   "psd": res["psd"] or {"n_bins": 0, "db": []}, "band": res["band"],
                   "band_rule_hits": res["band_hits"], "acquisition": block.meta}
            if cfg.save_iq_every_n and block.seq % cfg.save_iq_every_n == 0:
                path = Path(cfg.data_dir) / "iq" / f"{st}_{block.ts_ns}.cf32"
                block.samples.astype("<c8").tofile(path)
                raw["iq_file"] = str(path)
            docs.append(("raw_observations", raw, "insert"))

        for c in res["channels"]:
            keep = keep_all or block.seq % cfg.measurement_every_n_blocks == 0 or c["verdict"] != "normal"
            mid = f"{st}|{block.ts_ns}|{c['name']}|m"
            detection = {"verdict": c["verdict"], "rule_codes": [h["code"] for h in c["rule_hits"]],
                         "anomaly": c["ml"] and {k: c["ml"][k] for k in ("model", "score", "is_anomaly")},
                         "classification": c["ml"] and c["ml"].get("label") and
                         {"label": c["ml"]["label"], "probability": c["ml"]["probability"]}}
            if keep:
                docs.append(("measurements", {
                    "_id": mid, **common, "raw_id": raw_id,
                    "channel": {"name": c["name"], "freq_hz": c["freq_hz"]},
                    "features": c["features"], "band": res["band"], "detection": detection,
                    "label": c["truth"], "label_source": "simulation" if c["truth"] is not None else None,
                }, "insert"))
            if c["verdict"] != "normal":
                docs.append(("detections", {
                    "_id": f"{st}|{block.ts_ns}|{c['name']}|d", **common, "measurement_id": mid,
                    "channel": {"name": c["name"], "freq_hz": c["freq_hz"]}, "verdict": c["verdict"],
                    "rule_hits": c["rule_hits"], "anomaly": detection["anomaly"],
                    "classification": detection["classification"], "mitigations": c["mitigations"],
                    "features_snapshot": {k: c["features"][k] for k in
                                          ("snr_db", "carrier_offset_hz", "unexpected_peaks", "fsk_activity",
                                           "noise_floor_db")},
                    "ground_truth": c["truth"],
                }, "insert"))
        return docs

    # ----------------------------------------------------------------- run
    def process(self, block: IQBlock) -> dict:
        res = self.analyzer.analyze(block)
        docs = self.build_documents(block, res)
        added = self.buffer.put_many(docs)
        self.stats["blocks"] += 1
        self.stats["docs_buffered"] += added
        if not res["quality"]["valid"]:
            self.stats["invalid_blocks"] += 1
            log.warning("invalid block", extra={"seq": block.seq, "reasons": res["quality"]["reasons"]})
        for c in res["channels"]:
            self.stats["verdicts"][c["verdict"]] += 1
            if c["verdict"] == "anomalous":
                log.warning("anomaly detected", extra={"channel": c["name"], "seq": block.seq,
                                                       "rules": [h["code"] for h in c["rule_hits"]],
                                                       "ml": c["ml"]})
        if self.uploader is not None and self.buffer.count() >= self.cfg.batch_size:
            self.uploader.trigger()
        return res

    def status(self) -> dict:
        return {"updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                "station_id": self.cfg.station_id, "device_id": self.device_id, "source": self.source.name,
                "collector": self.stats, "source_stats": self.source.stats(),
                "buffer": {"rows": self.buffer.count(), "by_collection": self.buffer.count_by_collection(),
                           "dead_letter": self.buffer.dead_letter_count(), "oldest_age_s": self.buffer.oldest_age_s(),
                           "pruned_rows": self.buffer.pruned_rows},
                "uploader": dict(self.uploader.stats) if self.uploader else None}

    def write_status(self) -> None:
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status(), default=str, indent=2))
        tmp.replace(self.status_path)

    def run(self, stop: threading.Event | None = None, max_blocks: int | None = None) -> dict:
        stop = stop or threading.Event()
        self.buffer.put_many(self.metadata_docs())
        log.info("collector starting", extra={"device_id": self.device_id, "settings": self.cfg.safe_dict()})
        self.source.start()
        if self.uploader is not None and not self.uploader.is_alive():
            self.uploader.start()
        timeout = max(5.0, 3 * self.cfg.block_seconds)
        try:
            while not stop.is_set():
                if max_blocks is not None and self.stats["blocks"] >= max_blocks:
                    break
                try:
                    block = self.source.read_block(timeout=timeout)
                except EOFError:
                    log.info("source exhausted")
                    break
                if block is None:
                    self.stats["timeouts"] += 1
                    log.warning("no data from receiver", extra={"timeout_s": timeout,
                                                                "timeouts": self.stats["timeouts"]})
                    continue
                try:
                    self.process(block)
                    self._consecutive_errors = 0
                except Exception:
                    self.stats["errors"] += 1
                    self._consecutive_errors += 1
                    log.exception("failed to process block", extra={"seq": block.seq})
                    if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise RuntimeError("too many consecutive processing errors") from None
                if time.monotonic() - self._last_heartbeat >= self.cfg.heartbeat_s:
                    self._last_heartbeat = time.monotonic()
                    st = self.status()
                    log.info("heartbeat", extra={"blocks": self.stats["blocks"], "verdicts": self.stats["verdicts"],
                                                 "buffer_rows": st["buffer"]["rows"],
                                                 "uploader": st["uploader"], "source": st["source_stats"]})
                    self.write_status()
        finally:
            log.info("collector stopping", extra={"blocks": self.stats["blocks"]})
            self.source.stop()
            if self.uploader is not None:
                self.uploader.stop()
            self.write_status()
        return self.stats
