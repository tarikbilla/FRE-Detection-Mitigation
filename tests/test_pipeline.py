"""End-to-end: simulated receiver -> analysis -> buffer -> uploader -> (mongomock) MongoDB."""
import json

from fre_platform.pipeline import Collector
from fre_platform.sources.simulated import SimulatedSource
from fre_platform.storage.buffer import LocalBuffer
from fre_platform.storage.uploader import Uploader


def test_end_to_end_research_dataset(cfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, cfg)
    src = SimulatedSource(cfg, scenario="mixed", seed=5, anomaly_probability=0.6)
    col = Collector(cfg, src, buf, up, keep_all_observations=True)
    stats = col.run(max_blocks=40)

    db = mongomock_store.db
    assert stats["blocks"] == 40 and stats["errors"] == 0
    assert buf.count() == 0, "buffer must be drained after final flush"
    # Clean research schema: only the four research collections exist, no detections/models/etc.
    assert set(db.list_collection_names()) <= {"sessions", "observations", "spectra", "telegrams"}
    assert db.observations.count_documents({}) == 40 * len(cfg.channels)
    assert db.spectra.count_documents({}) == 40          # keep_all -> one spectrum per block
    assert db.sessions.count_documents({}) == 1

    sess = db.sessions.find_one({"_id": cfg.experiment_id})
    assert sess["device"]["receive_only"] and sess["feature_names"] and sess["config"]["channels"]

    o = db.observations.find_one({"channel.name": "DCF49"})
    assert o["schema_version"] == 1 and o["label"] is not None and o["label_source"] == "simulation"
    assert o["ts"].tzinfo is not None and "snr_db" in o["features"] and o["signal_state"] in ("present", "lost", "absent")
    assert "detection" not in o   # detection disabled by default

    sp = db.spectra.find_one()
    assert len(sp["psd"]["db"]) == cfg.psd_bins and sp["quality"]["valid"]

    status = json.loads((cfg.data_dir / "status.json").read_text())
    assert status["collector"]["blocks"] == 40 and status["buffer"]["rows"] == 0


def test_telegrams_decoded_and_stored(cfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, cfg)
    src = SimulatedSource(cfg, scenario="normal", seed=5, emit_telegrams=True)
    col = Collector(cfg, src, buf, up, keep_all_observations=True)
    stats = col.run(max_blocks=60)

    db = mongomock_store.db
    assert stats["telegrams"] > 0 and stats["telegrams_time"] > 0
    tg = db.telegrams.find_one({"kind": "time"})
    assert tg is not None and tg["checksum_ok"] and tg["evu_address"] == "0000"
    assert tg["time"]["year"] >= 2024 and tg["raw_hex"].startswith("68")
    # telegram _ids are deterministic -> re-running does not duplicate
    before = db.telegrams.count_documents({})
    up.flush_once()
    assert db.telegrams.count_documents({}) == before


def test_spoofed_time_telegrams_flagged(cfg, tmp_path, mongomock_store):
    """A replay/spoof run (wrong encoded time) is marked implausible in the dataset."""
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, cfg)
    src = SimulatedSource(cfg, scenario="normal", seed=5, emit_telegrams=True, spoof_time_offset_s=3600)
    col = Collector(cfg, src, buf, up, keep_all_observations=True)
    stats = col.run(max_blocks=60)

    db = mongomock_store.db
    assert stats["telegrams_time"] > 0 and stats["telegrams_suspect"] > 0
    tg = db.telegrams.find_one({"kind": "time"})
    assert tg["integrity"]["assessment"] == "implausible"
    assert abs(tg["integrity"]["clock_offset_s"] - 3600) < 5 and tg["integrity"]["time_plausible"] is False
    # authentic vs spoofed is queryable for research
    assert db.telegrams.count_documents({"integrity.assessment": "implausible"}) > 0


def test_detection_enabled_adds_verdicts(dcfg, tmp_path, mongomock_store):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    up = Uploader(buf, lambda: mongomock_store, dcfg)
    src = SimulatedSource(dcfg, scenario="jamming_wideband", seed=5)
    col = Collector(dcfg, src, buf, up, keep_all_observations=True)
    col.run(max_blocks=20)
    o = mongomock_store.db.observations.find_one({"channel.name": "DCF49"})
    assert "detection" in o and o["detection"]["verdict"] in ("normal", "suspicious", "anomalous")


def test_live_output(cfg, tmp_path):
    from fre_platform.cli import format_live
    seen = []
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    col = Collector(cfg, SimulatedSource(cfg, seed=2, emit_telegrams=True), buf, on_block=seen.append)
    col.run(max_blocks=30)
    assert [s["seq"] for s in seen] == list(range(30))
    live = json.loads((cfg.data_dir / "live.json").read_text())
    assert live["seq"] == 29 and len(live["channels"]) == len(cfg.channels)
    text = format_live(live)
    assert "DCF49" in text and "SNR" in text


def test_sampling_reduces_volume_for_live_data(cfg, tmp_path):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    src = SimulatedSource(cfg, scenario="normal", seed=6)
    col = Collector(cfg, src, buf, uploader=None, keep_all_observations=False)
    col.run(max_blocks=20)
    counts = buf.count_by_collection()
    # spectrum every 600th block, observations every 60th -> almost nothing in 20 blocks
    assert counts.get("spectra", 0) <= 1
    assert counts.get("observations", 0) <= 1 * len(cfg.channels)


def test_invalid_block_is_recorded_not_fatal(cfg, tmp_path):
    import numpy as np
    buf = LocalBuffer(tmp_path / "b.sqlite3")

    class BadSource(SimulatedSource):
        def read_block(self, timeout=5.0):
            b = super().read_block(timeout)
            if b.seq == 1:
                b.samples = np.full_like(b.samples, np.nan)
            return b

    col = Collector(cfg, BadSource(cfg, seed=1, max_blocks=3), buf, keep_all_observations=True)
    stats = col.run()
    assert stats["blocks"] == 3 and stats["invalid_blocks"] == 1 and stats["errors"] == 0
