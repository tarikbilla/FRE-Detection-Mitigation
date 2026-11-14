import os

import pytest

from fre_platform.config import Settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Tests never read the developer's real .env / service env file."""
    atlas = os.environ.get("FRE_RUN_ATLAS_TESTS") == "1"
    for k in list(os.environ):
        if not k.startswith("FRE_") or k in ("FRE_RUN_ATLAS_TESTS", "FRE_TEST_HARDWARE"):
            continue
        # opt-in Atlas tests need the connection settings (URI or parts, or the env file)
        if atlas and (k == "FRE_ENV_FILE" or k.startswith("FRE_MONGODB_")):
            continue
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cfg(tmp_path):
    return Settings.from_env(env={}, source="simulated", block_seconds=0.5, baseline_min_blocks=6,
                             data_dir=tmp_path / "data", model_path=tmp_path / "models" / "m.joblib",
                             station_id="test-station", batch_size=50, upload_interval_s=0.05,
                             max_backoff_s=0.2, heartbeat_s=3600)


@pytest.fixture
def dcfg(cfg):
    """Config with the detection/ML stack enabled (phase-2 behaviour)."""
    import dataclasses
    return dataclasses.replace(cfg, enable_detection=True)


@pytest.fixture
def mongomock_store():
    mongomock = pytest.importorskip("mongomock")
    from fre_platform.storage.mongo import MongoStore
    return MongoStore(client=mongomock.MongoClient(tz_aware=True), db_name="fre_test")


def warmed_source(cfg, analyzer, seed=500, n=None):
    """A simulated source that has already fed ``analyzer`` a normal baseline."""
    from fre_platform.sources.simulated import SimulatedSource
    src = SimulatedSource(cfg, scenario="normal", seed=seed)
    for _ in range(n or cfg.baseline_min_blocks + 2):
        analyzer.analyze(src.read_block())
    return src
