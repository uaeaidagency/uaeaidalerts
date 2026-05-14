"""
UAE Aid Agency — Tier-2+ Alert Agent
Submission watcher.

Reads `06_Pending_Submissions.csv`, compares against the persisted set
of submission IDs in `state/seen_submissions.json`, and returns the
rows representing newly-uploaded projects (those awaiting approval,
deferral, or rejection by the relevant approver).

The dashboard's "Submit for Approval" modal exports a fresh
`06_Pending_Submissions.csv` snapshot on every submission; the user
saves the download into the dashboard folder (overwriting the previous
copy). Each hour the agent rescans and fires alerts for any submission
IDs (PA-YYYY-###) it hasn't seen before.

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
SUBMISSIONS_CSV = os.path.join(PARENT, "06_Pending_Submissions.csv")
STATE_DIR = os.path.join(HERE, "state")
STATE_FILE = os.path.join(STATE_DIR, "seen_submissions.json")

# Mirrors CRISIS_LOCATIONS in the dashboard (UAE_Humanitarian_Dashboard_Live.html).
# Maps country → list of major crisis hotspot names within that country.
# Used as a fallback when the CSV pre-dates the "Crisis Location" column.
_CRISIS_LOCATIONS: dict = {
    "Sudan":               ["Khartoum", "El Fasher", "Port Sudan"],
    "Palestine (Gaza)":    ["Northern Gaza", "Gaza City"],
    "Afghanistan":         ["Kabul"],
    "DR Congo":            ["Goma"],
    "Yemen":               ["Sanaa", "Hodeidah"],
    "Haiti":               ["Port-au-Prince"],
    "Syrian Arab Republic":["Aleppo", "Idlib"],
    "Somalia":             ["Mogadishu", "Baidoa"],
    "Ethiopia":            ["Mekelle"],
    "Ukraine":             ["Kharkiv", "Zaporizhzhia"],
    "Lebanon":             ["Beirut"],
    "Bangladesh":          ["Cox's Bazar"],
    "Pakistan":            ["Larkana"],
    "Burkina Faso":        ["Ouagadougou", "Djibo"],
    "Türkiye":             ["Kahramanmaraş"],
    "Indonesia":           ["Palu, Sulawesi"],
    "Mozambique":          ["Pemba"],
    "Niger":               ["Niamey"],
    "Mali":                ["Bamako"],
    "South Sudan":         ["Juba"],
    "Myanmar":             ["Rakhine State", "Sagaing"],
}


def _location_of(country: str) -> str:
    """Return the major crisis location(s) within a country, or empty string if unknown."""
    locs = _CRISIS_LOCATIONS.get(country.strip(), [])
    return " / ".join(locs) if locs else ""


@dataclass
class Submission:
    submission_id: str
    submitted_date: str
    country: str
    location: str
    tier: str
    project_name: str
    trigger: str
    proposed_modality: str
    proposed_amount_usd: float
    implementer: str
    duration: str
    point_of_contact: str
    approver: str
    submitted_by: str
    notes: str


def _load_state() -> Optional[Set[str]]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids") or [])
    except Exception:
        return set()


def _save_state(seen_ids: Set[str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "seen_ids": sorted(seen_ids),
            },
            f,
            indent=2,
        )


def _safe_float(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def _load_submissions() -> List[Submission]:
    if not os.path.exists(SUBMISSIONS_CSV):
        return []
    rows: List[Submission] = []
    with open(SUBMISSIONS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if not any((v or "").strip() for v in r.values()):
                continue
            sid = (r.get("Submission ID") or "").strip()
            if not sid:
                continue
            rows.append(
                Submission(
                    submission_id=sid,
                    submitted_date=(r.get("Submitted Date") or "").strip(),
                    country=(r.get("Country") or "").strip(),
                    location=(r.get("Crisis Location") or "").strip() or _location_of((r.get("Country") or "").strip()),
                    tier=(r.get("Tier") or "").strip(),
                    project_name=(r.get("Project Name") or "").strip(),
                    trigger=(r.get("Trigger") or "").strip(),
                    proposed_modality=(r.get("Proposed Modality") or "").strip(),
                    proposed_amount_usd=_safe_float(r.get("Proposed Amount (USD)")),
                    implementer=(r.get("Implementer") or "").strip(),
                    duration=(r.get("Duration") or "").strip(),
                    point_of_contact=(r.get("Point of Contact") or "").strip(),
                    approver=(r.get("Approver") or "").strip(),
                    submitted_by=(r.get("Submitted By") or "").strip(),
                    notes=(r.get("Notes") or "").strip(),
                )
            )
    return rows


def detect_new_submissions(force_all: bool = False) -> List[Submission]:
    """Return submissions not previously seen.

    First run: seed state with current rows and return [].
    """
    subs = _load_submissions()
    if force_all:
        return subs

    seen = _load_state()
    if seen is None:
        _save_state({s.submission_id for s in subs})
        return []

    return [s for s in subs if s.submission_id not in seen]


def mark_seen(subs: List[Submission]) -> None:
    if not subs:
        return
    seen = _load_state() or set()
    seen.update(s.submission_id for s in subs)
    _save_state(seen)


def to_dict(s: Submission) -> dict:
    return asdict(s)


if __name__ == "__main__":
    import argparse, json as _json
    p = argparse.ArgumentParser(description="List submissions.")
    p.add_argument("--force-all", action="store_true",
                   help="List every submission in the CSV (ignore state).")
    args = p.parse_args()
    rows = detect_new_submissions(force_all=args.force_all)
    print(f"{len(rows)} submission(s):")
    for r in rows:
        print(_json.dumps(to_dict(r), indent=2))
