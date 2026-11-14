import pytest

from fre_platform.detection.ml import ML_FEATURES, FreModels, generate_simulated_dataset, train_models
from fre_platform.pipeline import BlockAnalyzer
from fre_platform.sources.simulated import SimulatedSource


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    from fre_platform.config import Settings
    tmp = tmp_path_factory.mktemp("ml")
    cfg = Settings.from_env(env={}, source="simulated", block_seconds=0.5, baseline_min_blocks=6,
                            data_dir=tmp / "d", model_path=tmp / "m.joblib")
    df = generate_simulated_dataset(cfg, blocks_per_scenario=12, seed=3)
    models, report = train_models(df, "test-v1", seed=1)
    return cfg, df, models, report


def test_dataset_is_labelled(trained):
    _, df, _, _ = trained
    assert set(ML_FEATURES) <= set(df.columns)
    assert {"normal", "spoof", "jamming_wideband", "flood"} <= set(df["label"])


def test_classifier_quality(trained):
    _, _, _, report = trained
    assert report["metadata"]["classifier_accuracy"] >= 0.8
    assert report["metadata"]["anomaly_model"]["detection_rate"] >= 0.5


def test_save_load_predict(trained, tmp_path):
    import dataclasses
    cfg, _, models, _ = trained
    cfg = dataclasses.replace(cfg, enable_detection=True)  # ML runs only when detection is enabled
    path = tmp_path / "m.joblib"
    models.save(path)
    loaded = FreModels.load(path)
    an = BlockAnalyzer(cfg, models=loaded)
    res = an.analyze(SimulatedSource(cfg, scenario="normal", seed=5).read_block())
    ml = res["channels"][1]["ml"]
    assert set(ml) >= {"score", "is_anomaly", "label", "probability"}
    assert loaded.metadata["dataset_version"] == "test-v1"


def test_training_requires_labels(trained):
    _, df, _, _ = trained
    with pytest.raises(ValueError):
        train_models(df.drop(columns=["label"]), "x")
