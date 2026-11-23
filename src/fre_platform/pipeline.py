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
from .detection.ml import FreModels
from .detection.rules import RuleEngine, RuleHit, verdict_from
from .dsp.features import BAND_FEATURES, CHANNEL_FEATURES, FeatureExtractor, assess_quality
from .mitigation.advisor import recommend
from .protocol.telegram import assess_authenticity
from .sources.base import IQBlock, IQSource, ns_to_datetime
from .storage.buffer import LocalBuffer
from .timeid import make_id

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
        self.detect = cfg.enable_detection
        self.rules = RuleEngine(cfg) if self.detect else None
        self.models = models if self.detect else None
        self.demods: dict[str, "FskDemodulator"] = {}
        self._tele_bytes: dict[str, bytes] = {}
        if cfg.decode_telegrams:
            from .dsp.demod import FskDemodulator
            for ch in cfg.channels:
                self.demods[ch.name] = FskDemodulator(self.extractor.fs_bb)
                self._tele_bytes[ch.name] = b""
        self._prev_ts: int | None = None
        self._ntp: bool | None = ntp_synced()
        self._ntp_checked = time.monotonic()

    def _decode_telegrams(self, channel_name: str, baseband) -> list[dict]:
        demod = self.demods.get(channel_name)
        if demod is None:
            return []
        data = demod.process(baseband)
        buf = self._tele_bytes[channel_name] + data
        if len(buf) < 5:                       # shortest possible frame
            self._tele_bytes[channel_name] = buf
            return []
        from .protocol.framing import parse_stream
        from .protocol.telegram import classify
        # Buffer bytes across blocks so a frame split over a block boundary is reassembled.
        frames, self._tele_bytes[channel_name] = parse_stream(
            buf, require_checksum=self.cfg.telegram_require_checksum)
        return [classify(f).to_dict() for f in frames]

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
        if self.detect:
            base_floor = self.rules.band_baseline.median() if self.rules.band_baseline.ready else None
            band["band_floor_rise_db"] = round(band["band_noise_floor_db"] - base_floor, 3) if base_floor is not None else 0.0
            band_hits = self.rules.evaluate_band(band, quality)
        else:
            band["band_floor_rise_db"] = 0.0
            band_hits = []
        result.update(band={k: round(v, 5) for k, v in band.items()}, psd=psd,
                      band_hits=[h.to_dict() for h in band_hits])
        shared = [h for h in band_hits if h.severity != "info"]

        chans = []
        for ch in self.cfg.channels:
            baseband = self.extractor.baseband(block, ch)
            feats = self.extractor.channel(block, ch, band, baseband=baseband)
            telegrams = self._decode_telegrams(ch.name, baseband)
            for tg in telegrams:
                tg["integrity"] = assess_authenticity(
                    tg, block.ts, signal=feats, ntp_synced=quality.get("ntp_synced"),
                    tolerance_s=self.cfg.time_plausibility_tolerance_s)
            established = self.rules.channel_established(ch.name) if self.detect else feats["snr_db"] >= self.cfg.snr_min_db
            signal_state = "present" if feats["snr_db"] >= self.cfg.snr_min_db else (
                "lost" if established else "absent")
            entry = {"name": ch.name, "freq_hz": ch.freq_hz, "features": feats,
                     "ml_input": {**feats, "band_flatness": band["band_flatness"],
                                  "band_floor_rise_db": band["band_floor_rise_db"]},
                     "truth": (block.truth or {}).get(ch.name), "established": established,
                     "signal_state": signal_state, "telegrams": telegrams, "verdict": "normal",
                     "rule_hits": [], "ml": None, "mitigations": []}
            if self.detect:
                entry["hits"] = shared + self.rules.evaluate_channel(ch.name, feats, band_hits)
            chans.append(entry)

        if self.detect:
            ml_out = self.models.predict([c["ml_input"] for c in chans]) if self.models else [None] * len(chans)
            for c, ml in zip(chans, ml_out):
                ml_anom = bool(ml and ml.get("is_anomaly"))
                ml_label = ml.get("label") if ml else None
                ml_escalates = c["established"] and (ml_anom or ml_label not in (None, "normal"))
                verdict = verdict_from(c["hits"], ml_escalates)
                codes = [h.code for h in c["hits"] if h.severity != "info"]
                c.update(rule_hits=[h.to_dict() for h in c.pop("hits")], ml=ml, verdict=verdict,
                         mitigations=recommend(codes, ml_escalates and ml_anom,
                                               ml_label if ml_escalates else None) if verdict != "normal" else [])
        result["channels"] = chans
        return result


class Collector:
    def __init__(self, cfg: Settings, source: IQSource, buffer: LocalBuffer, uploader=None,
                 models: FreModels | None = None, keep_all_observations: bool | None = None,
                 on_block=None, owns_uploader: bool = True):
        self.cfg = cfg
        # False when the integrated service shares one uploader between the SDR
        # collector, relay monitor and IR probe (the service starts/stops it).
        self.owns_uploader = owns_uploader
        self.source = source
        self.buffer = buffer
        self.uploader = uploader
        self.analyzer = BlockAnalyzer(cfg, models)
        self.models = models if cfg.enable_detection else None
        # Keep every observation (no decimation) - default on for simulation runs
        # so a labelled dataset is dense; live collection decimates by default.
        self.keep_all = keep_all_observations
        info = source.device_info()
        serial = info.get("serial") or source.name
        self.device_id = f"{cfg.station_id}:{source.name}:{serial}"
        self.device_info = info
        self.stats = {"blocks": 0, "invalid_blocks": 0, "errors": 0, "timeouts": 0, "docs_buffered": 0,
                      "observations": 0, "spectra": 0, "telegrams": 0, "telegrams_time": 0,
                      "telegrams_suspect": 0,
                      "verdicts": {"normal": 0, "suspicious": 0, "anomalous": 0},
                      "started_at": datetime.now(timezone.utc).isoformat()}
        self._consecutive_errors = 0
        self._last_heartbeat = time.monotonic()
        self.status_path = Path(cfg.data_dir) / "status.json"
        self.live_path = Path(cfg.data_dir) / "live.json"
        self.on_block = on_block  # optional callback(summary) for console output
        (Path(cfg.data_dir) / "iq").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- live
    def live_summary(self, block: IQBlock, res: dict) -> dict:
        """Compact per-block summary for the console / ``fre-platform watch``."""
        up = self.uploader.stats if self.uploader else None
        return {
            "ts": block.ts.isoformat(timespec="milliseconds"), "seq": block.seq, "source": block.source,
            "valid": res["quality"]["valid"], "reasons": res["quality"]["reasons"],
            "band_floor_db": res["band"].get("band_noise_floor_db"),
            "band_power_dbfs": res["band"].get("band_total_power_dbfs"),
            "band_rules": [h["code"] for h in res["band_hits"]],
            "channels": [{
                "name": c["name"], "freq_hz": c["freq_hz"], "verdict": c["verdict"],
                "signal_state": c["signal_state"],
                "power_dbfs": c["features"]["power_dbfs"], "snr_db": c["features"]["snr_db"],
                "offset_hz": c["features"]["carrier_offset_hz"], "fsk_activity": c["features"]["fsk_activity"],
                "lines": c["features"]["unexpected_peaks"],
                "rules": [h["code"] for h in c["rule_hits"]],
                "ml": (c["ml"] or {}).get("label"),
                "telegrams": [{"kind": t["kind"], "evu": t.get("evu_address"), "checksum_ok": t["checksum_ok"],
                               "time": (t.get("time") or {}).get("iso"), "raw_hex": t["raw_hex"],
                               "assessment": t.get("integrity", {}).get("assessment"),
                               "clock_offset_s": t.get("integrity", {}).get("clock_offset_s")}
                              for t in c["telegrams"]],
            } for c in res["channels"]],
            "telegram_totals": {"total": self.stats["telegrams"], "time": self.stats["telegrams_time"],
                                "suspect": self.stats["telegrams_suspect"]},
            "buffer_rows": self.buffer.count(),
            "upload": None if up is None else {"online": up["online"], "uploaded": up["uploaded"],
                                               "storage_mb": up.get("storage_used_mb"),
                                               "restricted": up.get("quota_restricted", False)},
        }

    def _write_live(self, summary: dict) -> None:
        try:
            tmp = self.live_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(summary, default=str))
            tmp.replace(self.live_path)
        except OSError:  # live view is best-effort; never break collection
            log.debug("could not write live.json", exc_info=True)

    # ------------------------------------------------------------ metadata
    def _common(self, block: IQBlock) -> dict:
        return {"schema_version": SCHEMA_VERSION, "dataset_version": self.cfg.dataset_version,
                "experiment_id": self.cfg.experiment_id, "station_id": self.cfg.station_id,
                "source": block.source, "ts": block.ts, "ts_ns": block.ts_ns, "seq": block.seq,
                "software_version": __version__}

    def session_doc(self) -> tuple[str, dict, str]:
        """One reproducibility record per experiment (device + conditions + schema)."""
        cfg, now = self.cfg, datetime.now(timezone.utc)
        doc = {
            "_id": cfg.experiment_id,
            "schema_version": SCHEMA_VERSION, "dataset_version": cfg.dataset_version,
            "station_id": cfg.station_id, "description": cfg.experiment_description,
            "software_version": __version__, "started_at": now, "updated_at": now,
            "device": {"id": self.device_id, "type": self.source.name, "receive_only": True,
                       "model": self.device_info.get("model"), "serial": self.device_info.get("serial"),
                       "firmware": self.device_info.get("firmware"), "info": self.device_info},
            "host": {"hostname": socket.gethostname(), "platform": platform.platform(),
                     "machine": platform.machine(), "python": platform.python_version()},
            "authorization": {"mode": "receive-only", "transmission": "none",
                              "note": "passive observation and authorized simulation only"},
            "config": {"antenna": cfg.antenna, "location_label": cfg.location_label, "source": cfg.source,
                       "center_freq_hz": cfg.center_freq_hz, "sample_rate_hz": cfg.sample_rate_hz,
                       "block_seconds": cfg.block_seconds, "baseband_rate_hz": cfg.baseband_rate_hz,
                       "channels": [{"name": c.name, "freq_hz": c.freq_hz} for c in cfg.channels],
                       "airspy": {"agc": cfg.airspy_agc, "attenuation": cfg.airspy_attenuation, "lna": cfg.airspy_lna},
                       "decode_telegrams": cfg.decode_telegrams, "detection_enabled": cfg.enable_detection,
                       "sampling": {"observation_every_n_blocks": cfg.observation_every_n_blocks,
                                    "spectrum_every_n_blocks": cfg.spectrum_every_n_blocks, "psd_bins": cfg.psd_bins},
                       "scenario": getattr(self.source, "scenario", None)},
            "feature_names": list(CHANNEL_FEATURES), "band_feature_names": list(BAND_FEATURES),
            "telegram_signal": {"modulation": "FSK", "shift_hz": 170, "baud": 200, "coding": "8E1",
                                "frame": "DIN 19244 / FT1.2 variable"},
        }
        return ("sessions", doc, "upsert")

    # ----------------------------------------------------------- documents
    def build_documents(self, block: IQBlock, res: dict) -> list[tuple[str, dict, str]]:
        cfg = self.cfg
        common = self._common(block)
        st = cfg.station_id
        docs: list[tuple[str, dict, str]] = []
        keep_all = self.keep_all if self.keep_all is not None else block.truth is not None
        alert = any(c["verdict"] != "normal" for c in res["channels"]) if cfg.enable_detection else False

        # Band spectrum + block quality (frequency-domain research record)
        if block.seq % cfg.spectrum_every_n_blocks == 0 or keep_all or alert or not res["quality"]["valid"]:
            spec = {"_id": make_id(block.ts, st, "spectrum"), **common,
                    "center_freq_hz": float(block.center_freq_hz), "sample_rate_hz": int(block.sample_rate_hz),
                    "psd": res["psd"] or {"n_bins": 0, "db": []}, "band": res["band"],
                    "quality": res["quality"], "acquisition": block.meta}
            docs.append(("spectra", spec, "insert"))

        for c in res["channels"]:
            # Per-channel feature observation (the ML-ready dataset record)
            if keep_all or block.seq % cfg.observation_every_n_blocks == 0 or alert:
                obs = {"_id": make_id(block.ts, st, c["name"]), **common,
                       "channel": {"name": c["name"], "freq_hz": c["freq_hz"]},
                       "features": c["features"], "signal_state": c["signal_state"],
                       "quality": {k: res["quality"][k] for k in ("valid", "rms_dbfs", "clip_fraction",
                                                                  "ntp_synced", "dropped_blocks_before")},
                       "label": c["truth"], "label_source": "simulation" if c["truth"] is not None else None}
                if cfg.enable_detection:
                    obs["detection"] = {"verdict": c["verdict"], "rule_codes": [h["code"] for h in c["rule_hits"]],
                                        "ml_label": (c["ml"] or {}).get("label")}
                docs.append(("observations", obs, "insert"))

            # Decoded received telegrams (the actual signal content) + authenticity features
            for i, tg in enumerate(c["telegrams"]):
                docs.append(("telegrams", {
                    "_id": make_id(block.ts, st, c["name"], i), **common,
                    "channel": {"name": c["name"], "freq_hz": c["freq_hz"]},
                    "received_ts": block.ts, **tg,
                    "label_source": "simulation" if block.truth is not None else None,
                }, "insert"))
        return docs

    # ----------------------------------------------------------------- run
    def process(self, block: IQBlock) -> dict:
        res = self.analyzer.analyze(block)
        docs = self.build_documents(block, res)
        added = self.buffer.put_many(docs)
        self.stats["blocks"] += 1
        self.stats["docs_buffered"] += added
        self.stats["observations"] += sum(1 for coll, _, _ in docs if coll == "observations")
        self.stats["spectra"] += sum(1 for coll, _, _ in docs if coll == "spectra")
        if not res["quality"]["valid"]:
            self.stats["invalid_blocks"] += 1
            log.warning("invalid block", extra={"seq": block.seq, "reasons": res["quality"]["reasons"]})
        for c in res["channels"]:
            for tg in c["telegrams"]:
                self.stats["telegrams"] += 1
                if tg["kind"] == "time":
                    self.stats["telegrams_time"] += 1
                integ = tg.get("integrity", {})
                if integ.get("assessment") in ("implausible", "integrity_fail"):
                    self.stats["telegrams_suspect"] += 1
                    log.warning("suspect telegram", extra={"channel": c["name"], "seq": block.seq,
                                                           "kind": tg["kind"], "assessment": integ.get("assessment"),
                                                           "clock_offset_s": integ.get("clock_offset_s"),
                                                           "checksum_ok": tg["checksum_ok"]})
                log.info("telegram received", extra={"channel": c["name"], "seq": block.seq, "kind": tg["kind"],
                                                     "evu": tg.get("evu_address"), "checksum_ok": tg["checksum_ok"],
                                                     "assessment": integ.get("assessment"),
                                                     "decoded_time": (tg.get("time") or {}).get("iso")})
            if self.cfg.enable_detection:
                self.stats["verdicts"][c["verdict"]] += 1
                if c["verdict"] == "anomalous":
                    log.warning("anomaly detected", extra={"channel": c["name"], "seq": block.seq,
                                                           "rules": [h["code"] for h in c["rule_hits"]], "ml": c["ml"]})
        if self.uploader is not None and self.buffer.count() >= self.cfg.batch_size:
            self.uploader.trigger()
        summary = self.live_summary(block, res)
        self._write_live(summary)
        if self.on_block is not None:
            self.on_block(summary)
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
        self.buffer.put_many([self.session_doc()])
        log.info("collector starting", extra={"device_id": self.device_id, "settings": self.cfg.safe_dict()})
        self.source.start()
        if self.uploader is not None and self.owns_uploader and not self.uploader.is_alive():
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
            if self.uploader is not None and self.owns_uploader:
                self.uploader.stop()
            self.write_status()
        return self.stats
