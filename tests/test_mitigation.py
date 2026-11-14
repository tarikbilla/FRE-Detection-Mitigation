import pandas as pd

from fre_platform.mitigation.simulator import evaluate_strategies, run_mitigation_experiment


def test_evaluate_strategies_deterministic():
    ev = pd.DataFrame([
        {"seq": 1, "channel": "A", "truth": "normal", "verdict": "normal", "fsk_activity": 0.3},
        {"seq": 2, "channel": "A", "truth": "spoof", "verdict": "anomalous", "fsk_activity": 0.6},
        {"seq": 3, "channel": "A", "truth": "spoof", "verdict": "normal", "fsk_activity": 0.6},
        {"seq": 4, "channel": "A", "truth": "normal", "verdict": "suspicious", "fsk_activity": 0.3},
        {"seq": 5, "channel": "A", "truth": "jamming_wideband", "verdict": "anomalous", "fsk_activity": 0.3},
        {"seq": 6, "channel": "A", "truth": "normal", "verdict": "normal", "fsk_activity": 0.0},
    ])
    t = evaluate_strategies(ev).set_index("strategy")
    assert t.loc["naive", "attack_accept_rate"] == 1.0 and t.loc["naive", "legit_accept_rate"] == 1.0
    assert t.loc["detector_gated", "attack_accept_rate"] == 0.5
    assert t.loc["detector_gated", "legit_accept_rate"] == 0.5
    assert t.loc["naive", "attack_telegrams"] == 2 and t.loc["naive", "legit_telegrams"] == 2


def test_detection_gating_reduces_attack_acceptance(cfg):
    table, events = run_mitigation_experiment(cfg, blocks=80, seed=3)
    t = table.set_index("strategy")
    assert t.loc["detector_gated", "attack_accept_rate"] < t.loc["naive", "attack_accept_rate"]
    assert t.loc["gated_rate_limited", "attack_accept_rate"] <= t.loc["detector_gated", "attack_accept_rate"]
    assert len(events) == 80 * len(cfg.channels)
