"""
UAE Aid Agency — Tier-2 Alert Agent
Scoring module.

Implements the Priority Score and Decision Tier computation defined in
`UAE_Humanitarian_Monitoring_Methodology.md` section 4.

Priority Score = 0.30 * MandateFit
              + 0.25 * Severity (1-5) * 20
              + 0.20 * min(100, log10(PIN_thousands) * 25)
              + 0.15 * (100 - AppealFunded%)
              + 0.10 * Access(1-5) * 20

MandateFit  = base(priority tier: 1=80, 2=60, 3=40, not listed=10)
            + 4 per past approved engagement (capped at +20)

Decision Tier:  >=75 = 1   |  60-74 = 2   |  45-59 = 3   |  30-44 = 4   |  <30 = 5

DEPENDENCIES: Standard library only.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)  # the dashboard folder
CRISES_CSV = os.path.join(PARENT, "01_Active_Crises.csv")
INDICATORS_CSV = os.path.join(PARENT, "02_Humanitarian_Indicators.csv")
PRIORITY_CSV = os.path.join(PARENT, "03_UAE_Priority_Countries.csv")
DECISIONS_CSV = os.path.join(PARENT, "04_Response_Decision_Log.csv")

PRIORITY_BASE = {1: 80, 2: 60, 3: 40}


@dataclass
class CrisisScore:
    crisis_id: str
    country: str
    region: str
    crisis_type: str
    status: str
    severity: int
    pin_thousands: float
    appeal_funded_pct: float
    access_constraints: int
    priority_tier: Optional[int]
    track_record_count: int
    mandate_fit: float
    severity_score: float
    affected_pop_score: float
    response_gap_score: float
    media_political_score: float
    priority_score: float
    decision_tier: int
    trigger_event: str
    last_updated: str
    disease_outbreak_risk: int
    displaced_thousands: float
    casualties_thousands: float
    ipc_phase3_thousands: float
    children_malnourished_thousands: float
    health_facilities_damaged_pct: float
    shelter_needs_thousands: float
    wash_below_std_pct: float


def _load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # Strip rows that are entirely blank (the decision log has trailing empties).
    return [r for r in rows if any((v or "").strip() for v in r.values())]


def _safe_float(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def _safe_int(v: str, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def _compute_priority_score(
    priority_tier: Optional[int],
    track_record: int,
    severity: int,
    pin_thousands: float,
    appeal_funded_pct: float,
    access: int,
) -> Dict[str, float]:
    base = PRIORITY_BASE.get(priority_tier, 10) if priority_tier else 10
    bonus = min(20, max(0, track_record) * 4)
    mandate_fit = min(100.0, base + bonus)

    severity_score = max(0, min(5, severity)) * 20.0

    if pin_thousands > 0:
        affected_pop_score = min(100.0, math.log10(pin_thousands) * 25.0)
    else:
        affected_pop_score = 0.0
    affected_pop_score = max(0.0, affected_pop_score)

    response_gap_score = max(0.0, min(100.0, 100.0 - appeal_funded_pct))

    media_political_score = max(0, min(5, access)) * 20.0

    composite = (
        0.30 * mandate_fit
        + 0.25 * severity_score
        + 0.20 * affected_pop_score
        + 0.15 * response_gap_score
        + 0.10 * media_political_score
    )
    return {
        "mandate_fit": round(mandate_fit, 2),
        "severity_score": round(severity_score, 2),
        "affected_pop_score": round(affected_pop_score, 2),
        "response_gap_score": round(response_gap_score, 2),
        "media_political_score": round(media_political_score, 2),
        "priority_score": round(composite, 2),
    }


def _priority_to_tier(score: float) -> int:
    if score >= 75:
        return 1
    if score >= 60:
        return 2
    if score >= 45:
        return 3
    if score >= 30:
        return 4
    return 5


def compute_all_scores() -> List[CrisisScore]:
    """Read the four source CSVs and produce a CrisisScore for every active crisis."""
    crises = _load_csv(CRISES_CSV)
    indicators = {r["Crisis ID"]: r for r in _load_csv(INDICATORS_CSV)}
    priority_countries = {r["Country"]: _safe_int(r["Tier (1-3)"]) for r in _load_csv(PRIORITY_CSV)}

    track_record: Dict[str, int] = {}
    for r in _load_csv(DECISIONS_CSV):
        country = (r.get("Country") or "").strip()
        decision = (r.get("Decision") or "").strip().lower()
        if country and decision == "approve":
            track_record[country] = track_record.get(country, 0) + 1

    results: List[CrisisScore] = []
    for c in crises:
        cid = c["Crisis ID"].strip()
        country = c["Country"].strip()
        ind = indicators.get(cid, {})
        severity = _safe_int(c.get("Severity (1-5)"))
        pin = _safe_float(ind.get("People in Need (PIN '000)"))
        funded = _safe_float(ind.get("Appeal Funded (%)"))
        access = _safe_int(ind.get("Access Constraints (1-5)"))

        scores = _compute_priority_score(
            priority_tier=priority_countries.get(country),
            track_record=track_record.get(country, 0),
            severity=severity,
            pin_thousands=pin,
            appeal_funded_pct=funded,
            access=access,
        )
        tier = _priority_to_tier(scores["priority_score"])

        results.append(
            CrisisScore(
                crisis_id=cid,
                country=country,
                region=(c.get("Region") or "").strip(),
                crisis_type=(c.get("Crisis Type") or "").strip(),
                status=(c.get("Status") or "").strip(),
                severity=severity,
                pin_thousands=pin,
                appeal_funded_pct=funded,
                access_constraints=access,
                priority_tier=priority_countries.get(country),
                track_record_count=track_record.get(country, 0),
                mandate_fit=scores["mandate_fit"],
                severity_score=scores["severity_score"],
                affected_pop_score=scores["affected_pop_score"],
                response_gap_score=scores["response_gap_score"],
                media_political_score=scores["media_political_score"],
                priority_score=scores["priority_score"],
                decision_tier=tier,
                trigger_event=(c.get("Notes / Trigger Event") or "").strip(),
                last_updated=(c.get("Last Updated") or "").strip(),
                disease_outbreak_risk=_safe_int(ind.get("Disease Outbreak Risk (1-5)")),
                displaced_thousands=_safe_float(ind.get("Displaced (IDPs+Refugees '000)")),
                casualties_thousands=_safe_float(ind.get("Casualties ('000)")),
                ipc_phase3_thousands=_safe_float(ind.get("IPC Phase 3+ ('000)")),
                children_malnourished_thousands=_safe_float(
                    ind.get("Children Malnourished ('000)")
                ),
                health_facilities_damaged_pct=_safe_float(
                    ind.get("Health Facilities Damaged (%)")
                ),
                shelter_needs_thousands=_safe_float(ind.get("Shelter Needs ('000)")),
                wash_below_std_pct=_safe_float(ind.get("WASH Access Below Std (%)")),
            )
        )
    return results


def to_dict(s: CrisisScore) -> dict:
    return asdict(s)


def _print_summary() -> None:
    """Print a compact per-crisis summary sorted by Priority Score (desc)."""
    scores = sorted(compute_all_scores(), key=lambda s: s.priority_score, reverse=True)
    print(f"{'ID':<6} {'Country':<22} {'Tier':<5} {'Score':>7} "
          f"{'Sev':>4} {'PIN(K)':>8} {'Fund%':>6} {'Acc':>4}")
    print("-" * 70)
    for s in scores:
        print(
            f"{s.crisis_id:<6} {s.country:<22} "
            f"  T{s.decision_tier:<2} {s.priority_score:>7.1f} "
            f"{s.severity:>4} {s.pin_thousands:>8,.0f} "
            f"{s.appeal_funded_pct:>5.0f}% {s.access_constraints:>4}"
        )
    tier_counts = {}
    for s in scores:
        tier_counts[s.decision_tier] = tier_counts.get(s.decision_tier, 0) + 1
    print()
    print("Tier counts: " + ", ".join(
        f"T{t} = {tier_counts.get(t, 0)}" for t in sorted(tier_counts)
    ))


if __name__ == "__main__":
    import sys as _sys
    if "--json" in _sys.argv:
        import json as _json
        for s in compute_all_scores():
            print(_json.dumps(to_dict(s), indent=2))
    else:
        _print_summary()
