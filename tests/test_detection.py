import pytest

from fre_platform.detection.rules import RobustBaseline, RuleHit, verdict_from
from fre_platform.mitigation.advisor import recommend
from fre_platform.pipeline import BlockAnalyzer

from .conftest import warmed_source


def run_scenario(cfg, scenario, blocks=8, seed=9):
    an = BlockAnalyzer(cfg)
    src = warmed_source(cfg, an, seed=seed)
    src.set_scenario(scenario)
    return [an.analyze(src.read_block()) for _ in range(blocks)]


def codes(results, channel="DCF49"):
    out = set()
    for r in results:
        for c in r["channels"]:
            if c["name"] == channel:
                out |= {h["code"] for h in c["rule_hits"]}
    return out


@pytest.mark.parametrize("scenario,expected", [
    ("jamming_wideband", "WIDEBAND_JAMMING"),
    ("jamming_narrowband", "NARROWBAND_INTERFERENCE"),
    ("freq_offset", "FREQ_OFFSET"),
    ("carrier_loss", "CARRIER_LOSS"),
    ("flood", "TELEGRAM_FLOOD"),
    ("spoof", "MULTIPLE_EMITTERS"),
])
def test_rules_detect_scenarios(cfg, scenario, expected):
    assert expected in codes(run_scenario(cfg, scenario))


def test_normal_has_low_false_alarm_rate(cfg):
    res = run_scenario(cfg, "normal", blocks=30, seed=21)
    verdicts = [c["verdict"] for r in res for c in r["channels"]]
    assert verdicts.count("anomalous") / len(verdicts) <= 0.05


def test_attack_does_not_poison_baseline(cfg):
    an = BlockAnalyzer(cfg)
    src = warmed_source(cfg, an, seed=4)
    before = an.rules.band_baseline.median()
    src.set_scenario("jamming_wideband")
    for _ in range(20):
        an.analyze(src.read_block())
    assert abs(an.rules.band_baseline.median() - before) < 1.0


def test_robust_baseline():
    b = RobustBaseline(window=50, min_n=5)
    assert b.zscore(100) == 0.0  # not ready yet
    for v in [10, 11, 9, 10, 10, 11, 9]:
        b.update(v)
    assert b.ready and abs(b.zscore(10)) < 1 and b.zscore(40) > 6


def test_verdicts_and_mitigations():
    assert verdict_from([]) == "normal"
    assert verdict_from([], ml_anomaly=True) == "suspicious"
    assert verdict_from([RuleHit("X", "info", "")]) == "normal"
    assert verdict_from([RuleHit("X", "critical", "")]) == "anomalous"
    recs = recommend(["MULTIPLE_EMITTERS", "NARROWBAND_INTERFERENCE"])
    actions = [r["action"] for r in recs]
    assert "quarantine_telegrams" in actions and len(actions) == len(set(actions))
    # recommendations are receiver-side/organisational only - never offensive RF actions
    assert not [a for a in actions if a.startswith(("transmit", "jam", "send", "inject", "replay"))]
    assert recommend([], ml_anomaly=True)[0]["action"] == "analyst_review"
