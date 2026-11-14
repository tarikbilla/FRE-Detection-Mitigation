"""Offline evaluation of receiver-side mitigation strategies (simulation only).

A population of *simulated* ripple-control receivers consumes abstract
"telegram events" derived from simulated observations. Each block/channel
with FSK activity is treated as one received telegram; its ground-truth
label says whether it is legitimate (normal) or attacker-originated (spoof,
flood). Jamming/carrier-loss blocks deliver no usable telegram (denial).

Strategies compared:

* ``naive``          - accept every received telegram;
* ``detector_gated`` - accept only when the detection layer says "normal";
* ``gated_rate_limited`` - detector gating plus a per-channel cap on accepted
  telegrams within a sliding window.

Outputs attack-acceptance and legitimate-acceptance rates. No real telegram
format is decoded or produced.
"""
from __future__ import annotations

from collections import defaultdict, deque

import pandas as pd

from ..config import Settings

MALICIOUS = {"spoof", "flood"}
DENIAL = {"jamming_wideband", "jamming_narrowband", "carrier_loss"}
TELEGRAM_ACTIVITY_MIN = 0.05


def evaluate_strategies(events: pd.DataFrame, rate_limit: int = 3, window_blocks: int = 10) -> pd.DataFrame:
    """``events`` columns: seq, channel, truth, verdict, fsk_activity."""
    ev = events[events["fsk_activity"] >= TELEGRAM_ACTIVITY_MIN].copy()
    ev = ev[~ev["truth"].isin(DENIAL)]
    ev["malicious"] = ev["truth"].isin(MALICIOUS)
    results = []
    for name in ("naive", "detector_gated", "gated_rate_limited"):
        windows: dict[str, deque] = defaultdict(deque)
        accepted = []
        for row in ev.sort_values("seq").itertuples():
            ok = True
            if name != "naive" and row.verdict != "normal":
                ok = False
            if ok and name == "gated_rate_limited":
                w = windows[row.channel]
                while w and w[0] <= row.seq - window_blocks:
                    w.popleft()
                if len(w) >= rate_limit:
                    ok = False
                else:
                    w.append(row.seq)
            accepted.append(ok)
        ev[name] = accepted
        mal, leg = ev[ev["malicious"]], ev[~ev["malicious"]]
        results.append({
            "strategy": name,
            "attack_telegrams": int(len(mal)),
            "attack_accept_rate": round(float(mal[name].mean()), 4) if len(mal) else None,
            "legit_telegrams": int(len(leg)),
            "legit_accept_rate": round(float(leg[name].mean()), 4) if len(leg) else None,
        })
    return pd.DataFrame(results)


def run_mitigation_experiment(cfg: Settings, blocks: int = 200, seed: int = 11, models=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the simulated 'mixed' scenario through the full detection stack."""
    import dataclasses

    from ..pipeline import BlockAnalyzer
    from ..sources.simulated import SimulatedSource

    cfg = dataclasses.replace(cfg, enable_detection=True)  # strategy comparison needs detector verdicts
    src = SimulatedSource(cfg, scenario="mixed", seed=seed, anomaly_probability=0.35)
    analyzer = BlockAnalyzer(cfg, models=models)
    rows = []
    for _ in range(blocks):
        res = analyzer.analyze(src.read_block())
        for ch in res["channels"]:
            rows.append({"seq": res["seq"], "channel": ch["name"], "truth": ch["truth"],
                         "verdict": ch["verdict"], "fsk_activity": ch["features"]["fsk_activity"]})
    events = pd.DataFrame(rows)
    return evaluate_strategies(events), events
