"""Interpretable rule-based detection against a robust rolling baseline.

Rules only ever *flag* observations; they never act on the radio side.
Baselines are updated only with blocks that raised no warning/critical hit,
so an ongoing attack cannot slowly become the new "normal".
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass

import numpy as np

from ..config import Settings

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
PERSIST_BLOCKS = 3
NARROWBAND_PEAKS = 6
FSK_FLOOR_ALLOWANCE_DB = 10.0


@dataclass
class RuleHit:
    code: str
    severity: str  # info | warning | critical
    detail: str
    value: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("value", "threshold"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 4)
        return d


class RobustBaseline:
    """Rolling median / MAD statistics for one scalar."""

    def __init__(self, window: int, min_n: int):
        self.values: deque[float] = deque(maxlen=window)
        self.min_n = min_n

    @property
    def ready(self) -> bool:
        return len(self.values) >= self.min_n

    def update(self, v: float | None) -> None:
        if v is not None and np.isfinite(v):
            self.values.append(float(v))

    def median(self) -> float:
        return float(np.median(self.values)) if self.values else float("nan")

    def zscore(self, v: float) -> float:
        if not self.ready:
            return 0.0
        arr = np.asarray(self.values)
        med = np.median(arr)
        mad = np.median(np.abs(arr - med)) * 1.4826
        return float((v - med) / max(mad, 0.5))  # floor avoids divide-by-~0 on very stable signals


def verdict_from(hits: list[RuleHit], ml_anomaly: bool = False) -> str:
    level = max((SEVERITY_ORDER[h.severity] for h in hits), default=-1)
    if level >= 2:
        return "anomalous"
    if level == 1 or ml_anomaly:
        return "suspicious"
    return "normal"


class RuleEngine:
    BASELINE_KEYS = ("power_dbfs", "noise_floor_db", "snr_db", "fsk_activity")

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        mk = lambda: RobustBaseline(cfg.baseline_window, cfg.baseline_min_blocks)  # noqa: E731
        self.band_baseline = mk()
        self.baselines: dict[str, dict[str, RobustBaseline]] = defaultdict(
            lambda: {k: mk() for k in self.BASELINE_KEYS})
        self.persist: dict[tuple[str, str], int] = defaultdict(int)

    def _persisted(self, channel: str, code: str, condition: bool) -> bool:
        key = (channel, code)
        self.persist[key] = self.persist[key] + 1 if condition else 0
        return self.persist[key] >= PERSIST_BLOCKS

    # ---------------------------------------------------------------- band
    def evaluate_band(self, band: dict, quality: dict) -> list[RuleHit]:
        hits: list[RuleHit] = []
        if quality.get("clip_fraction", 0) > 1e-3:
            hits.append(RuleHit("ADC_CLIPPING", "warning", "receiver input clipping; increase attenuation",
                                quality["clip_fraction"], 1e-3))
        if quality.get("dropped_blocks_before"):
            hits.append(RuleHit("DATA_GAP", "info", "blocks dropped before this one (processing backlog)",
                                quality["dropped_blocks_before"], 0))
        if quality.get("timestamp_reanchored"):
            hits.append(RuleHit("TIMESTAMP_REANCHORED", "info", "sample clock re-anchored to wall clock"))
        floor = band.get("band_noise_floor_db")
        if floor is not None and self.band_baseline.ready:
            rise = floor - self.band_baseline.median()
            if rise > self.cfg.jam_floor_rise_db:
                hits.append(RuleHit("WIDEBAND_JAMMING", "critical",
                                    "band noise floor far above baseline (wideband interference/jamming)",
                                    rise, self.cfg.jam_floor_rise_db))
        if not any(h.severity != "info" for h in hits):
            self.band_baseline.update(floor)
        return hits

    # ------------------------------------------------------------- channel
    def evaluate_channel(self, name: str, f: dict, band_hits: list[RuleHit] | None = None) -> list[RuleHit]:
        cfg = self.cfg
        b = self.baselines[name]
        hits: list[RuleHit] = []
        snr_ok = f["snr_db"] >= cfg.snr_min_db
        wideband = any(h.code == "WIDEBAND_JAMMING" for h in (band_hits or []))

        # carrier presence
        absent = self._persisted(name, "absent", not snr_ok)
        if absent:
            if b["snr_db"].ready and b["snr_db"].median() > cfg.snr_min_db + 10:
                hits.append(RuleHit("CARRIER_LOSS", "warning", "previously strong carrier is missing",
                                    f["snr_db"], cfg.snr_min_db))
            else:
                hits.append(RuleHit("SIGNAL_ABSENT", "info", "no signal above SNR threshold",
                                    f["snr_db"], cfg.snr_min_db))

        # frequency error (persistent, so random FSK bit imbalance does not trigger it)
        off = f["carrier_offset_hz"]
        if self._persisted(name, "freq", snr_ok and abs(off) > cfg.freq_tol_hz):
            hits.append(RuleHit("FREQ_OFFSET", "warning", "carrier persistently off its nominal frequency",
                                off, cfg.freq_tol_hz))

        # interference / additional emitters
        # Many stray lines, or a raised floor without coherent extra lines, is
        # interference. A few stray lines (a second FSK line set) is another
        # emitter - immediately if it also raises the floor, else if persistent.
        # An FSK telegram's own modulation sidebands lift the in-channel floor
        # (~+20 dB for a strong signal), so the threshold is raised while FSK
        # activity is present. A rise of 2.5x the threshold is interference
        # regardless of how many discrete lines are resolved.
        up = f["unexpected_peaks"]
        floor_rise = f["noise_floor_db"] - b["noise_floor_db"].median() if b["noise_floor_db"].ready else 0.0
        allowance = FSK_FLOOR_ALLOWANCE_DB if f["fsk_activity"] > 0.02 else 0.0
        raised = not wideband and floor_rise > cfg.jam_floor_rise_db + allowance
        severe = not wideband and floor_rise > 2.5 * cfg.jam_floor_rise_db + allowance
        emitter_lines = snr_ok and 2 <= up < NARROWBAND_PEAKS
        emitters_persist = self._persisted(name, "emitters", emitter_lines)
        if up >= NARROWBAND_PEAKS or severe or (raised and up < 2):
            hits.append(RuleHit("NARROWBAND_INTERFERENCE", "critical",
                                "many unexpected spectral lines or raised channel noise floor",
                                float(max(up, floor_rise)), float(NARROWBAND_PEAKS)))
        elif emitter_lines and (raised or emitters_persist):
            hits.append(RuleHit("MULTIPLE_EMITTERS", "critical",
                                "spectral lines inconsistent with a single FSK transmitter (possible rogue emitter)",
                                up, 2))

        # telegram activity
        act = f["fsk_activity"]
        if self._persisted(name, "flood", act > 0.85):
            hits.append(RuleHit("TELEGRAM_FLOOD", "warning", "continuous FSK activity far above normal duty cycle",
                                act, 0.85))

        # sudden received-power change
        if b["power_dbfs"].ready and not wideband:
            z = b["power_dbfs"].zscore(f["power_dbfs"])
            diff = f["power_dbfs"] - b["power_dbfs"].median()
            if abs(z) > cfg.zscore_threshold and abs(diff) > 6:
                hits.append(RuleHit("POWER_JUMP", "warning", "received power changed abruptly vs baseline",
                                    diff, 6))

        if not any(h.severity in ("warning", "critical") for h in hits) and not wideband:
            for k in self.BASELINE_KEYS:
                b[k].update(f.get(k))
        return hits

    def state(self) -> dict:
        return {
            "band_floor_median": self.band_baseline.median(),
            "channels": {ch: {k: {"n": len(v.values), "median": v.median()} for k, v in d.items()}
                         for ch, d in self.baselines.items()},
        }
