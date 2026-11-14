"""Defensive mitigation recommendations.

Recommendations are advisory records stored with each detection. They are
receiver-side / organisational measures only. The platform never transmits,
never jams, and never interacts with utility ripple-control equipment.
"""
from __future__ import annotations

PLAYBOOK: dict[str, list[dict]] = {
    "WIDEBAND_JAMMING": [
        {"action": "hold_last_valid_state", "rationale": "receivers should fail safe and keep the last validated "
                                                         "switching state / default schedule while the band is jammed"},
        {"action": "notify_operator", "rationale": "report interference to the grid operator and spectrum authority"},
        {"action": "record_iq_evidence", "rationale": "enable FRE_SAVE_IQ_EVERY_N temporarily to preserve evidence"},
    ],
    "NARROWBAND_INTERFERENCE": [
        {"action": "flag_channel_untrusted", "rationale": "do not trust telegrams decoded on this channel"},
        {"action": "cross_check_other_transmitter", "rationale": "confirm commands via an independent EFR channel"},
        {"action": "notify_operator", "rationale": "interference on a ripple-control frequency"},
    ],
    "MULTIPLE_EMITTERS": [
        {"action": "quarantine_telegrams", "rationale": "a second emitter on the channel may be a spoofing source; "
                                                        "hold telegrams for plausibility checks"},
        {"action": "require_plausibility_checks", "rationale": "accept only commands consistent with schedule, "
                                                               "rate limits and sequence expectations"},
        {"action": "cross_check_other_transmitter", "rationale": "legitimate commands should be confirmable"},
        {"action": "notify_operator", "rationale": "possible rogue transmitter"},
    ],
    "FREQ_OFFSET": [
        {"action": "flag_channel_untrusted", "rationale": "legitimate transmitters are frequency-stable"},
        {"action": "verify_receiver_calibration", "rationale": "compare against the DCF77 reference carrier"},
    ],
    "TELEGRAM_FLOOD": [
        {"action": "rate_limit_commands", "rationale": "cap accepted switching commands per time window"},
        {"action": "require_plausibility_checks", "rationale": "reject bursts of contradictory commands"},
    ],
    "CARRIER_LOSS": [
        {"action": "hold_last_valid_state", "rationale": "keep last validated state until the carrier returns"},
        {"action": "check_receiver_chain", "rationale": "rule out antenna/receiver faults before escalating"},
    ],
    "POWER_JUMP": [
        {"action": "correlate_with_reference", "rationale": "if DCF77 changed too it is propagation/receiver, "
                                                            "otherwise a local emitter may be present"},
    ],
    "ADC_CLIPPING": [
        {"action": "increase_attenuation", "rationale": "set FRE_AIRSPY_ATTENUATION higher or disable LNA"},
    ],
}

ML_ANOMALY = [{"action": "analyst_review", "rationale": "statistical anomaly without a matching rule"}]


def recommend(rule_codes: list[str], ml_anomaly: bool = False, ml_label: str | None = None) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    codes = list(rule_codes)
    if ml_label and ml_label != "normal":
        codes.append({"jamming_wideband": "WIDEBAND_JAMMING", "jamming_narrowband": "NARROWBAND_INTERFERENCE",
                      "spoof": "MULTIPLE_EMITTERS", "freq_offset": "FREQ_OFFSET", "flood": "TELEGRAM_FLOOD",
                      "carrier_loss": "CARRIER_LOSS"}.get(ml_label, ""))
    for code in codes:
        for rec in PLAYBOOK.get(code, []):
            if rec["action"] not in seen:
                seen.add(rec["action"])
                out.append({**rec, "trigger": code})
    if ml_anomaly and not out:
        out = [{**r, "trigger": "ML_ANOMALY"} for r in ML_ANOMALY]
    return out
