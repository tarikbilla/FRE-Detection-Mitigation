"""Machine-learning detection (scikit-learn).

* ``IsolationForest`` - unsupervised anomaly detector trained on *normal*
  observations only (simulated or a recorded, quiet baseline).
* ``RandomForestClassifier`` - supervised classifier over the labelled
  simulation scenarios.

Only scale-free features (SNR, offsets, relative levels, activity...) are
used, so a model trained on simulated data transfers better to a real
receiver whose absolute gain is unknown. Domain shift between simulation and
real-world data remains a documented limitation; retrain on labelled field
data (``fre-platform train --from-db``) as it becomes available.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .. import __version__
from ..config import Settings

log = logging.getLogger(__name__)

ML_FEATURES = (
    "snr_db", "carrier_offset_hz", "mark_rel_db", "space_rel_db", "center_rel_db", "fsk_index_db",
    "n_peaks", "unexpected_peaks", "active_fraction", "fsk_activity", "if_std_hz", "symbol_rate",
    "crest_factor_db", "amp_kurtosis", "floor_rel_band_db", "band_flatness", "band_floor_rise_db",
)


def to_matrix(rows: list[dict] | pd.DataFrame, names=ML_FEATURES) -> np.ndarray:
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    for n in names:
        if n not in df:
            df[n] = 0.0
    m = df[list(names)].astype(float).to_numpy()
    return np.nan_to_num(m, nan=0.0, posinf=0.0, neginf=0.0)


@dataclass
class FreModels:
    anomaly: Pipeline
    classifier: Pipeline | None
    features: tuple[str, ...] = ML_FEATURES
    metadata: dict = field(default_factory=dict)

    def predict(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        X = to_matrix(rows, self.features)
        scores = self.anomaly.decision_function(X)  # < 0 => anomalous
        flags = self.anomaly.predict(X) == -1
        out = [{"model": "isolation_forest", "score": round(float(s), 5), "is_anomaly": bool(a)}
               for s, a in zip(scores, flags)]
        if self.classifier is not None:
            proba = self.classifier.predict_proba(X)
            classes = self.classifier.classes_
            for o, p in zip(out, proba):
                i = int(np.argmax(p))
                o["label"] = str(classes[i])
                o["probability"] = round(float(p[i]), 4)
        return out

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"anomaly": self.anomaly, "classifier": self.classifier, "features": list(self.features),
                     "metadata": self.metadata}, path)

    @classmethod
    def load(cls, path: Path) -> "FreModels":
        obj = joblib.load(Path(path))
        meta = obj.get("metadata", {})
        if meta.get("sklearn_version") and meta["sklearn_version"] != sklearn.__version__:
            log.warning("model trained with scikit-learn %s, running %s", meta["sklearn_version"], sklearn.__version__)
        return cls(obj["anomaly"], obj.get("classifier"), tuple(obj["features"]), meta)


def generate_simulated_dataset(cfg: Settings, blocks_per_scenario: int = 40, seed: int = 7,
                               scenarios: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Labelled feature rows from the synthetic generator (no I/O, no transmission)."""
    import dataclasses

    from ..pipeline import BlockAnalyzer
    from ..sources.simulated import LABELS, SimulatedSource

    cfg = dataclasses.replace(cfg, enable_detection=True)  # baseline-relative features need the rule engine
    rows: list[dict] = []
    for i, sc in enumerate(scenarios or LABELS):
        analyzer = BlockAnalyzer(cfg, models=None)
        # Warm-up: the attack starts after a period of normal reception from
        # the same transmitters, as in the field, so baseline-relative
        # features are realistic.
        src = SimulatedSource(cfg, scenario="normal", seed=seed + i)
        for _ in range(cfg.baseline_min_blocks + 2):
            analyzer.analyze(src.read_block())
        src.set_scenario(sc)
        for _ in range(blocks_per_scenario):
            res = analyzer.analyze(src.read_block())
            for ch in res["channels"]:
                rows.append({**ch["ml_input"], "label": ch["truth"], "channel": ch["name"], "scenario": sc})
    return pd.DataFrame(rows)


def train_models(df: pd.DataFrame, dataset_version: str, seed: int = 42, source: str = "simulation") -> tuple[FreModels, dict]:
    if "label" not in df or df["label"].isna().all():
        raise ValueError("training data needs a 'label' column")
    df = df.dropna(subset=["label"]).reset_index(drop=True)
    X = to_matrix(df)
    y = df["label"].astype(str).to_numpy()
    strat = y if min(np.unique(y, return_counts=True)[1]) >= 2 else None
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=seed, stratify=strat)

    clf = Pipeline([("scale", StandardScaler()),
                    ("rf", RandomForestClassifier(n_estimators=150, class_weight="balanced", random_state=seed,
                                                  n_jobs=2, min_samples_leaf=2))])
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    labels = sorted(set(y))
    report = classification_report(yte, pred, labels=labels, output_dict=True, zero_division=0)

    normal_tr = Xtr[ytr == "normal"]
    if len(normal_tr) < 10:
        raise ValueError("need at least 10 'normal' rows to train the anomaly model")
    iso = Pipeline([("scale", StandardScaler()),
                    ("iso", IsolationForest(n_estimators=200, contamination=0.02, random_state=seed))])
    iso.fit(normal_tr)
    iso_flag = iso.predict(Xte) == -1
    is_attack = yte != "normal"
    iso_metrics = {
        "detection_rate": float(iso_flag[is_attack].mean()) if is_attack.any() else None,
        "false_positive_rate": float(iso_flag[~is_attack].mean()) if (~is_attack).any() else None,
    }

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "software_version": __version__,
        "sklearn_version": sklearn.__version__,
        "dataset_version": dataset_version,
        "training_source": source,
        "n_train": int(len(Xtr)), "n_test": int(len(Xte)),
        "labels": labels,
        "classifier_accuracy": round(float(report["accuracy"]), 4),
        "classifier_macro_f1": round(float(report["macro avg"]["f1-score"]), 4),
        "anomaly_model": {k: (round(v, 4) if v is not None else None) for k, v in iso_metrics.items()},
    }
    models = FreModels(iso, clf, ML_FEATURES, meta)
    full = {"metadata": meta, "classification_report": report,
            "confusion_matrix": {"labels": labels, "matrix": confusion_matrix(yte, pred, labels=labels).tolist()}}
    return models, full
