"""End-to-end: simulated receiver -> analysis -> buffer -> uploader -> (mongomock) MongoDB."""
import json

from fre_platform.pipeline import Collector
from fre_platform.sources.simulated import SimulatedSource
from fre_platform.storage.buffer import LocalBuffer
from fre_platform.storage.uploader import Uploader


def test_end_to_end_mixed_scenario(cfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, cfg)
    src = SimulatedSource(cfg, scenario="mixed", seed=5, anomaly_probability=0.6)
    col = Collector(cfg, src, buf, up, keep_all_measurements=True)
    stats = col.run(max_blocks=40)

    db = mongomock_store.db
    assert stats["blocks"] == 40 and stats["errors"] == 0
    assert buf.count() == 0, "buffer must be drained after final flush"
    assert db.measurements.count_documents({}) == 40 * len(cfg.channels)
    assert db.raw_observations.count_documents({}) == 40
    assert db.devices.count_documents({}) == 1 and db.experiments.count_documents({}) == 1
    assert db.datasets.find_one({"_id": cfg.dataset_version})["feature_names"]

    m = db.measurements.find_one({"channel.name": "DCF49"})
    assert m["schema_version"] == 1 and m["label"] is not None and m["label_source"] == "simulation"
    assert m["ts"].tzinfo is not None and "snr_db" in m["features"] and m["detection"]["verdict"]

    raw = db.raw_observations.find_one()
    assert len(raw["psd"]["db"]) == cfg.psd_bins and raw["quality"]["valid"]

    det = db.detections.find_one({"verdict": "anomalous"})
    assert det is not None and det["rule_hits"] and det["mitigations"]
    # every anomalous detection carries a ground-truth label from the simulator
    assert db.detections.count_documents({"ground_truth": None}) == 0

    status = json.loads((cfg.data_dir / "status.json").read_text())
    assert status["collector"]["blocks"] == 40 and status["buffer"]["rows"] == 0


def test_live_output(cfg, tmp_path):
    from fre_platform.cli import format_live
    seen = []
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    col = Collector(cfg, SimulatedSource(cfg, seed=2), buf, on_block=seen.append)
    col.run(max_blocks=3)
    assert [s["seq"] for s in seen] == [0, 1, 2]
    live = json.loads((cfg.data_dir / "live.json").read_text())
    assert live["seq"] == 2 and len(live["channels"]) == len(cfg.channels)
    text = format_live(live)
    assert text.count("\n") == len(cfg.channels) and "DCF49" in text and "SNR" in text


def test_sampling_reduces_volume_for_live_data(cfg, tmp_path):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    src = SimulatedSource(cfg, scenario="normal", seed=6)
    col = Collector(cfg, src, buf, uploader=None, keep_all_measurements=False)
    col.run(max_blocks=20)
    counts = buf.count_by_collection()
    # raw every 10th block, measurements every 5th block (unless anomalous)
    assert counts.get("raw_observations", 0) <= 2 + counts.get("detections", 0)
    assert counts.get("measurements", 0) <= 4 * len(cfg.channels) + counts.get("detections", 0)


def test_invalid_block_is_recorded_not_fatal(cfg, tmp_path):
    import numpy as np
    buf = LocalBuffer(tmp_path / "b.sqlite3")

    class BadSource(SimulatedSource):
        def read_block(self, timeout=5.0):
            b = super().read_block(timeout)
            if b.seq == 1:
                b.samples = np.full_like(b.samples, np.nan)
            return b

    col = Collector(cfg, BadSource(cfg, seed=1, max_blocks=3), buf, keep_all_measurements=True)
    stats = col.run()
    assert stats["blocks"] == 3 and stats["invalid_blocks"] == 1 and stats["errors"] == 0
