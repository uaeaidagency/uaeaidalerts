"""
UAE Aid Agency — Humanitarian Aid Monitoring System
Automated CSV refresher for GitHub Actions.

Runs before run_check.py on every hourly GitHub Actions cycle and updates:

  01_Active_Crises.csv
    • Severity (1-5)      — updated for natural-disaster crises from GDACS
    • Last Updated        — set to today whenever any field changes
    • New rows            — high-severity events not yet in the CSV are appended
                           (flagged "REVIEW NEEDED" so a human can curate them)

  02_Humanitarian_Indicators.csv
    • Appeal Funded (%)   — updated from UN FTS per-country HRP data

Conservative rules
    • Never deletes existing rows.
    • Never overwrites manually-curated fields that APIs don't provide:
      PIN, Displaced, IPC Phase 3+, Children Malnourished,
      Health Facilities Damaged, Shelter Needs, WASH Access Below Std,
      Access Constraints, Casualties.
    • Only updates a field when the new API value differs meaningfully
      (avoids spurious git diffs on every run).

DEPENDENCIES: Standard library only.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE   = os.path.dirname(os.path.abspath(__file__))
CRISES_CSV     = os.path.join(HERE, "01_Active_Crises.csv")
INDICATORS_CSV = os.path.join(HERE, "02_Humanitarian_Indicators.csv")

TIMEOUT = 25
TODAY   = dt.date.today().isoformat()

# ── Canonical country-name aliases ───────────────────────────────────────
# Maps API name variants → canonical name used in our CSVs.
COUNTRY_ALIASES: Dict[str, str] = {
    "Syrian Arab Republic": "Syria",
    "Palestine": "Palestine (Gaza)",
    "occupied Palestinian territory": "Palestine (Gaza)",
    "State of Palestine": "Palestine (Gaza)",
    "Gaza Strip": "Palestine (Gaza)",
    "Congo, the Democratic Republic of the": "DR Congo",
    "Congo (DRC)": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Viet Nam": "Vietnam",
    "Lao People's Democratic Republic": "Laos",
    "Myanmar (Burma)": "Myanmar",
    "Korea, Democratic People's Republic of": "DPRK",
    "Türkiye": "Turkey",
    "Czechia": "Czech Republic",
    "Iran (Islamic Republic of)": "Iran",
    "Côte d'Ivoire": "Ivory Coast",
}

DISASTER_SUBTYPES = {
    "TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake",
    "DR": "Drought", "VO": "Volcano", "WF": "Wildfire", "TS": "Tsunami",
}

# Minimum GDACS severity (1-5) to consider adding a brand-new crisis row.
NEW_CRISIS_MIN_SEVERITY = 4


# ── HTTP helper ───────────────────────────────────────────────────────────
def _http_json(url: str, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "uae-aid-monitor/1.0 (UAE Aid Agency Humanitarian Monitor)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── CSV helpers ───────────────────────────────────────────────────────────
def _read_csv(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Return (fieldnames, rows). Strips BOM and blank rows."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows   = [r for r in reader if any((v or "").strip() for v in r.values())]
    return fields, rows


def _write_csv(path: str, fields: List[str], rows: List[Dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _norm(name: str) -> str:
    """Normalise a country name for fuzzy matching."""
    return COUNTRY_ALIASES.get(name.strip(), name.strip())


def _next_crisis_id(existing_rows: List[Dict]) -> str:
    nums = []
    for r in existing_rows:
        m = re.match(r"C-(\d+)", r.get("Crisis ID", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"C-{max(nums, default=0)+1:03d}"


# ── UN FTS — funding percentages ──────────────────────────────────────────
def fetch_fts_funding() -> Dict[str, float]:
    """Return {canonical_country: funded_pct} from the current-year HRP data."""
    year = dt.date.today().year
    funded: Dict[str, float] = {}
    for y in (year, year - 1):
        try:
            data = _http_json(f"https://api.hpc.tools/v1/public/plan/year/{y}")
            plans = data.get("data") if isinstance(data, dict) else data
            for p in (plans or []):
                req = (
                    (p.get("requirements") or {}).get("revisedRequirements")
                    or (p.get("requirements") or {}).get("originalRequirements")
                    or 0
                )
                fnd  = (p.get("funding") or {}).get("totalFunding") or 0
                locs = p.get("locations") or p.get("countries") or []
                raw  = (locs[0].get("name") if locs else None) or p.get("name") or ""
                country = _norm(raw)
                if req > 0 and country:
                    pct = round((fnd / req) * 100, 1)
                    funded[country] = pct
            if funded:
                break
        except Exception as e:
            print(f"  [FTS] {type(e).__name__}: {e}", file=sys.stderr)
    return funded


# ── GDACS — natural disaster alerts ──────────────────────────────────────
def fetch_gdacs_events() -> List[Dict]:
    """Return list of current Red/Orange GDACS alerts."""
    try:
        data = _http_json(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS"
            "?alertlevel=Red;Orange",
            timeout=30,
        )
        features = data.get("features") if isinstance(data, dict) else (data or [])
        out = []
        for f in (features or []):
            p = f.get("properties", f) or {}
            alert = (p.get("alertlevel") or "").lower()
            sev   = 5 if alert == "red" else 4
            etype = p.get("eventtype", "")
            sub   = DISASTER_SUBTYPES.get(etype, "Disaster")
            raw_country = (p.get("country") or "").strip()
            country = _norm(raw_country) if raw_country else None
            if not country:
                continue
            out.append({
                "country": country,
                "subType": sub,
                "severity": sev,
                "alert":   alert,
                "name":    p.get("eventname") or sub,
                "id":      f"GDACS-{p.get('eventid','?')}",
            })
        return out
    except Exception as e:
        print(f"  [GDACS] {type(e).__name__}: {e}", file=sys.stderr)
        return []


# ── ReliefWeb — current disasters ────────────────────────────────────────
def fetch_reliefweb_events() -> List[Dict]:
    """Return current ReliefWeb disaster entries."""
    try:
        url = (
            "https://api.reliefweb.int/v1/disasters"
            "?appname=uae-aid-monitor&limit=100&profile=list"
            "&filter[field]=status&filter[value]=current"
            "&sort[]=date.created:desc"
        )
        data = _http_json(url)
        out  = []
        for d in data.get("data", []):
            f = d.get("fields", {}) or {}
            countries = f.get("country") or []
            raw = (countries[0].get("name") if countries else None) \
                  or (f.get("primary_country") or {}).get("name") or ""
            country = _norm(raw)
            if not country or country == "Unknown":
                continue
            types  = f.get("type") or []
            ctype  = (types[0].get("name") if types else None) or "Disaster"
            out.append({
                "country": country,
                "type":    ctype,
                "name":    f.get("name") or ctype,
                "url":     f.get("url"),
            })
        return out
    except Exception as e:
        print(f"  [ReliefWeb] {type(e).__name__}: {e}", file=sys.stderr)
        return []


# ── Main update logic ─────────────────────────────────────────────────────
def update_crises() -> None:
    print(f"[{TODAY}] update_crises.py starting…")

    # Read existing CSVs
    c_fields, crises      = _read_csv(CRISES_CSV)
    i_fields, indicators  = _read_csv(INDICATORS_CSV)

    # Index by Crisis ID and by country (lowercase)
    crises_by_id      = {r["Crisis ID"]: r for r in crises}
    crises_by_country = {r["Country"].lower(): r for r in crises}
    indic_by_id       = {r["Crisis ID"]: r for r in indicators}

    changes = 0

    # ── 1. Update Appeal Funded (%) from FTS ─────────────────────────────
    print("  Fetching FTS funding data…", end=" ")
    fts = fetch_fts_funding()
    print(f"{len(fts)} plans")
    for crisis in crises:
        country = crisis["Country"]
        pct = fts.get(country)
        if pct is None:
            # Try partial match
            for k, v in fts.items():
                if k.lower() in country.lower() or country.lower() in k.lower():
                    pct = v
                    break
        if pct is None:
            continue
        cid  = crisis["Crisis ID"]
        indic = indic_by_id.get(cid)
        if not indic:
            continue
        old = indic.get("Appeal Funded (%)", "")
        try:
            old_f = float(old)
        except (ValueError, TypeError):
            old_f = None
        if old_f is None or abs(old_f - pct) >= 1.0:
            indic["Appeal Funded (%)"] = str(pct)
            crisis["Last Updated"] = TODAY
            changes += 1
            print(f"    FTS update: {country} funded% {old} → {pct}")

    # ── 2. Update severity for natural-disaster crises from GDACS ─────────
    print("  Fetching GDACS events…", end=" ")
    gdacs_events = fetch_gdacs_events()
    print(f"{len(gdacs_events)} alerts")

    new_gdacs_countries: set = set()
    for ev in gdacs_events:
        country = ev["country"]
        new_gdacs_countries.add(country.lower())
        existing = crises_by_country.get(country.lower())
        if existing:
            # Only update severity for non-conflict crises (disasters)
            ctype = existing.get("Crisis Type", "").lower()
            if "conflict" in ctype or "complex" in ctype:
                continue
            old_sev = existing.get("Severity (1-5)", "")
            new_sev = str(ev["severity"])
            if old_sev != new_sev:
                existing["Severity (1-5)"] = new_sev
                existing["Last Updated"]   = TODAY
                changes += 1
                print(f"    GDACS severity: {country} {old_sev} → {new_sev} ({ev['subType']})")
        else:
            # New crisis — add if high severity
            if ev["severity"] >= NEW_CRISIS_MIN_SEVERITY:
                new_id = _next_crisis_id(crises)
                print(f"    GDACS new crisis: {country} ({ev['subType']}, sev={ev['severity']}) → {new_id} [NEEDS REVIEW]")
                new_crisis = {f: "" for f in c_fields}
                new_crisis.update({
                    "Crisis ID":          new_id,
                    "Country":            country,
                    "Region":             "",
                    "Crisis Type":        "Disaster",
                    "Status":             "Active",
                    "Start Date":         TODAY,
                    "Severity (1-5)":     str(ev["severity"]),
                    "INFORM Risk":        "",
                    "Source":             "GDACS",
                    "Last Updated":       TODAY,
                    "Notes / Trigger Event": f"REVIEW NEEDED — {ev['subType']} ({ev['alert'].upper()} alert). Auto-detected by update_crises.py.",
                })
                crises.append(new_crisis)
                crises_by_country[country.lower()] = new_crisis

                new_indic = {f: "" for f in i_fields}
                new_indic.update({
                    "Crisis ID": new_id,
                    "Country":   country,
                })
                indicators.append(new_indic)
                indic_by_id[new_id] = new_indic
                changes += 1

    # ── 3. Check ReliefWeb for crises not in our CSV ──────────────────────
    print("  Fetching ReliefWeb events…", end=" ")
    rw_events = fetch_reliefweb_events()
    print(f"{len(rw_events)} events")

    seen_rw: set = set()
    for ev in rw_events:
        country = ev["country"]
        key = country.lower()
        if key in seen_rw or key in crises_by_country:
            seen_rw.add(key)
            continue
        seen_rw.add(key)
        # Only add if also confirmed by GDACS (double-source) or type is Conflict
        if key not in new_gdacs_countries and ev["type"].lower() not in ("conflict", "complex"):
            continue
        new_id = _next_crisis_id(crises)
        print(f"    ReliefWeb new crisis: {country} ({ev['type']}) → {new_id} [NEEDS REVIEW]")
        new_crisis = {f: "" for f in c_fields}
        new_crisis.update({
            "Crisis ID":             new_id,
            "Country":               country,
            "Region":                "",
            "Crisis Type":           ev["type"],
            "Status":                "Active",
            "Start Date":            TODAY,
            "Severity (1-5)":        "3",
            "INFORM Risk":           "",
            "Source":                "ReliefWeb",
            "Last Updated":          TODAY,
            "Notes / Trigger Event": f"REVIEW NEEDED — {ev['name']}. Auto-detected by update_crises.py.",
        })
        crises.append(new_crisis)
        crises_by_country[country.lower()] = new_crisis

        new_indic = {f: "" for f in i_fields}
        new_indic.update({"Crisis ID": new_id, "Country": country})
        indicators.append(new_indic)
        indic_by_id[new_id] = new_indic
        changes += 1

    # ── 4. Write back ─────────────────────────────────────────────────────
    if changes:
        _write_csv(CRISES_CSV,     c_fields, crises)
        _write_csv(INDICATORS_CSV, i_fields, indicators)
        print(f"  ✓ {changes} change(s) written to CSVs.")
    else:
        print("  ✓ No changes — CSVs already up to date.")

    print("update_crises.py done.")


if __name__ == "__main__":
    update_crises()
