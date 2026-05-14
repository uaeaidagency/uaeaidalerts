"""
UAE Aid Agency — Tier-2+ Alert Agent
Approval / sign-off watcher.

Reads `04_Response_Decision_Log.csv`, compares against the persisted set
of approval keys in `state/seen_approvals.json`, and returns the rows
representing newly-recorded approvals (Decision = "Approve").

State file format:
    {"updated_at": "ISO-8601", "seen_keys": ["2026-05-09|C-002|Palestine (Gaza)|Approve", ...]}

First-run behavior: if the state file does not exist, the watcher seeds
it with every current approval key and returns an empty list — no
backfill alerts. Every subsequent run alerts only on truly NEW rows.

DEPENDENCIES: Standard library only.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from dataclasses import dataclass, asdict
from typing import List, Optional, Set

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
DECISIONS_CSV = os.path.join(PARENT, "04_Response_Decision_Log.csv")
STATE_DIR = os.path.join(HERE, "state")
STATE_FILE = os.path.join(STATE_DIR, "seen_approvals.json")


@dataclass
class Approval:
    date: str
    crisis_id: str
    country: str
    tier: str
    decision: str
    modality: str
    amount_usd_thousands: float
    lead_partner: str
    approver: str
    notes: str

    @property
    def key(self) -> str:
        return f"{self.date}|{self.crisis_id}|{self.country}|{self.decision}"

    @property
    def amount_usd(self) -> float:
        return (self.amount_usd_thousands or 0) * 1000.0


def _load_state() -> Optional[Set[str]]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_keys") or [])
    except Exception:
        return set()


def _save_state(seen_keys: Set[str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "seen_keys": sorted(seen_keys),
            },
            f,
            indent=2,
        )


def _safe_float(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def _load_approvals() -> List[Approval]:
    if not os.path.exists(DECISIONS_CSV):
        return []
    rows: List[Approval] = []
    with open(DECISIONS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # Skip totally blank trailing rows.
            if not any((v or "").strip() for v in r.values()):
                continue
            decision = (r.get("Decision") or "").strip()
            if decision.lower() != "approve":
                continue
            rows.append(
                Approval(
                    date=(r.get("Date") or "").strip(),
                    crisis_id=(r.get("Crisis ID") or "").strip(),
                    country=(r.get("Country") or "").strip(),
                    tier=(r.get("Tier") or "").strip(),
                    decision=decision,
                    modality=(r.get("Modality") or "").strip(),
                    amount_usd_thousands=_safe_float(r.get("Amount (USD '000)")),
                    lead_partner=(r.get("Lead Partner") or "").strip(),
                    approver=(r.get("Approver") or "").strip(),
                    notes=(r.get("Notes") or "").strip(),
                )
            )
    return rows


def detect_new_approvals(force_all: bool = False) -> List[Approval]:
    """Return approvals not previously seen.

    On first ever run (no state file), seed silently and return [].
    If `force_all`, return every approval regardless of state (used by tests).
    """
    approvals = _load_approvals()
    if force_all:
        return approvals

    seen = _load_state()
    if seen is None:
        # First run: record everything currently in the log, alert on nothing.
        _save_state({a.key for a in approvals})
        return []

    new = [a for a in approvals if a.key not in seen]
    return new


def mark_seen(approvals: List[Approval]) -> None:
    """Persist successful deliveries by adding their keys to the seen set."""
    if not approvals:
        return
    seen = _load_state() or set()
    seen.update(a.key for a in approvals)
    _save_state(seen)


def to_dict(a: Approval) -> dict:
    return asdict(a)


if __name__ == "__main__":
    import argparse, json as _json
    p = argparse.ArgumentParser(description="List approvals.")
    p.add_argument("--force-all", action="store_true",
                   help="List every approval in the log (ignore state).")
    args = p.parse_args()
    rows = detect_new_approvals(force_all=args.force_all)
    print(f"{len(rows)} approval(s):")
    for r in rows:
        print(_json.dumps(to_dict(r), indent=2))
