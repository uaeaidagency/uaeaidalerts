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
    • People in Need (PIN '000)        — from UN FTS HRP beneficiary targets
    • Displaced (IDPs+Refugees '000)   — from UNHCR Population Statistics API
    • IPC Phase 3+ ('000)              — from IPC Global Platform API
    • Casualties ('000)                — from ACLED API (needs ACLED_API_KEY secret)
    • Disease Outbreak Risk (1-5)      — from WHO Disease Outbreak News API
    • Appeal Funded (%)                — from UN FTS per-country HRP data

Conservative rules
    • Never deletes existing rows.
    • Never overwrites fields when the new API value is blank/unavailable.
    • Only updates a field when the new API value differs meaningfully
      (avoids spurious git diffs on every run).
    • Fields not covered by any API are left untouched:
        Children Malnourished, Health Facilities Damaged,
        Shelter Needs, WASH Access Below Std, Access Constraints.

DEPENDENCIES: Standard library only.
              ACLED requires ACLED_API_KEY + ACLED_EMAIL env vars (GitHub Secrets).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE          = os.path.dirname(os.path.abspath(__file__))
CRISES_CSV    = os.path.join(HERE, "01_Active_Crises.csv")
INDICATORS_CSV = os.path.join(HERE, "02_Humanitarian_Indicators.csv")

TIMEOUT = 25
TODAY   = dt.date.today().isoformat()

# ── Canonical country-name aliases ───────────────────────────────────────
COUNTRY_ALIASES: Dict[str, str] = {
    "Syrian Arab Republic":                    "Syria",
    "Palestine":                               "Palestine (Gaza)",
    "occupied Palestinian territory":          "Palestine (Gaza)",
    "State of Palestine":                      "Palestine (Gaza)",
    "Gaza Strip":                              "Palestine (Gaza)",
    "Congo, the Democratic Republic of the":   "DR Congo",
    "Congo (DRC)":                             "DR Congo",
    "Democratic Republic of the Congo":        "DR Congo",
    "Viet Nam":                                "Vietnam",
    "Lao People's Democratic Republic":        "Laos",
    "Myanmar (Burma)":                         "Myanmar",
    "Korea, Democratic People's Republic of":  "DPRK",
    "Türkiye":                                 "Turkey",
    "Czechia":                                 "Czech Republic",
    "Iran (Islamic Republic of)":              "Iran",
    "Côte d'Ivoire":                           "Ivory Coast",
    "Central African Republic":                "Central African Republic",
    "South Sudan":                             "South Sudan",
    "Occupied Palestinian Territory":          "Palestine (Gaza)",
}

# ISO-3166-1 alpha-3 codes used by UNHCR and ACLED APIs
COUNTRY_ISO3: Dict[str, str] = {
    "Afghanistan":             "AFG",
    "Bangladesh":              "BGD",
    "Burkina Faso":            "BFA",
    "Cameroon":                "CMR",
    "Central African Republic":"CAF",
    "Chad":                    "TCD",
    "Colombia":                "COL",
    "DR Congo":                "COD",
    "Ethiopia":                "ETH",
    "Haiti":                   "HTI",
    "Indonesia":               "IDN",
    "Iraq":                    "IRQ",
    "Jordan":                  "JOR",
    "Kenya":                   "KEN",
    "Lebanon":                 "LBN",
    "Libya":                   "LBY",
    "Mali":                    "MLI",
    "Mozambique":              "MOZ",
    "Myanmar":                 "MMR",
    "Niger":                   "NER",
    "Nigeria":                 "NGA",
    "Pakistan":                "PAK",
    "Palestine (Gaza)":        "PSE",
    "Philippines":             "PHL",
    "Somalia":                 "SOM",
    "South Sudan":             "SSD",
    "Sudan":                   "SDN",
    "Syria":                   "SYR",
    "Turkey":                  "TUR",
    "Ukraine":                 "UKR",
    "Venezuela":               "VEN",
    "Yemen":                   "YEM",
    "Zimbabwe":                "ZWE",
}

DISASTER_SUBTYPES = {
    "TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake",
    "DR": "Drought", "VO": "Volcano", "WF": "Wildfire", "TS": "Tsunami",
}

# Minimum GDACS severity (1-5) to consider adding a brand-new crisis row.
NEW_CRISIS_MIN_SEVERITY = 4

# Minimum relative change before we overwrite an existing value (5 %).
MIN_RELATIVE_CHANGE = 0.05


# ── HTTP helper ───────────────────────────────────────────────────────────
def _http_json(url: str, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept":     "application/json",
            "User-Agent": "uae-aid-monitor/1.0 (UAE Aid Agency Humanitarian Monitor)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── CSV helpers ───────────────────────────────────────────────────────────
def _read_csv(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
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
    return COUNTRY_ALIASES.get(name.strip(), name.strip())


def _next_crisis_id(existing_rows: List[Dict]) -> str:
    nums = []
    for r in existing_rows:
        m = re.match(r"C-(\d+)", r.get("Crisis ID", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"C-{max(nums, default=0)+1:03d}"


def _meaningful_change(old_str: str, new_val: float) -> bool:
    """True when the update is worth writing (avoids trivial diffs)."""
    try:
        old = float(old_str)
    except (ValueError, TypeError):
        return True          # blank → always write
    if old == 0:
        return new_val != 0
    return abs(new_val - old) / abs(old) >= MIN_RELATIVE_CHANGE


# ── UN FTS — funding % + People in Need ──────────────────────────────────
def fetch_fts_data() -> Tuple[Dict[str, float], Dict[str, int]]:
    """
    Return:
      funded  — {canonical_country: funded_pct}
      pin     — {canonical_country: people_in_need_thousands}
    Both drawn from the current-year HRP plan list on api.hpc.tools.
    """
    year   = dt.date.today().year
    funded: Dict[str, float] = {}
    pin:    Dict[str, int]   = {}

    for y in (year, year - 1):
        try:
            data  = _http_json(f"https://api.hpc.tools/v1/public/plan/year/{y}")
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
                if not country:
                    continue

                # Funding %
                if req > 0:
                    funded[country] = round((fnd / req) * 100, 1)

                # People in Need — several possible fields in FTS payload
                ben   = p.get("beneficiaries") or p.get("beneficiary") or {}
                in_need = (
                    ben.get("inNeed")
                    or ben.get("totalInNeed")
                    or ben.get("targetedPopulation")
                    or p.get("peopleInNeed")
                    or p.get("totalInNeed")
                )
                if in_need and float(in_need) > 0:
                    # FTS stores raw numbers; convert to thousands
                    pin[country] = int(float(in_need) / 1000)

            if funded:
                break
        except Exception as e:
            print(f"  [FTS] {type(e).__name__}: {e}", file=sys.stderr)

    return funded, pin


# ── UNHCR — displacement (IDPs + refugees from each country) ─────────────
def fetch_unhcr_displacement() -> Dict[str, int]:
    """
    Return {canonical_country: total_displaced_thousands}
    where total = IDPs still inside the country + refugees who fled abroad.
    Source: UNHCR Population Statistics API (no key required).
    """
    year      = dt.date.today().year
    displaced: Dict[str, int] = {}

    try:
        url = (
            "https://api.unhcr.org/population/v1/population/"
            f"?limit=500&dataset=unhcrpop&displayType=totals"
            f"&years={year}&cf_type=ISO3"
        )
        data  = _http_json(url, timeout=30)
        items = data.get("items") or data.get("data") or []

        for item in items:
            raw = (item.get("coo_name") or item.get("country_of_origin_name") or "").strip()
            country = _norm(raw)
            if not country:
                continue
            # Sum IDPs (in-country) + refugees/asylum seekers (fled abroad)
            total = sum(
                int(item.get(k) or 0)
                for k in ("idps", "refugees", "asylum_seekers", "ooc", "hst")
            )
            if total > 0:
                displaced[country] = displaced.get(country, 0) + int(total / 1000)
    except Exception as e:
        print(f"  [UNHCR] {type(e).__name__}: {e}", file=sys.stderr)

    return displaced


# ── IPC — food-insecurity Phase 3+ population ────────────────────────────
def fetch_ipc_phases() -> Dict[str, int]:
    """
    Return {canonical_country: ipc_phase3_plus_thousands}.
    Tries the IPC Global Platform public API.
    """
    ipc: Dict[str, int] = {}
    try:
        data = _http_json(
            "https://www.ipcinfo.org/api/index.cfm?action=getCountries&format=json",
            timeout=30,
        )
        countries_list = data if isinstance(data, list) else data.get("countries") or data.get("data") or []
        for entry in countries_list:
            raw     = (entry.get("countryName") or entry.get("country") or "").strip()
            country = _norm(raw)
            if not country:
                continue
            # IPC API may return p3plus, phase3plus, total_phase3plus, population_phase3plus …
            p3 = (
                entry.get("p3plus")
                or entry.get("phase3plus")
                or entry.get("total_phase3plus")
                or entry.get("population_phase3plus")
                or entry.get("phase_3_plus")
            )
            if p3 and float(p3) > 0:
                ipc[country] = int(float(p3) / 1000)
    except Exception as e:
        print(f"  [IPC] {type(e).__name__}: {e}", file=sys.stderr)

    # Fallback: try individual country analyses for countries we track
    if not ipc:
        for country, iso3 in COUNTRY_ISO3.items():
            try:
                url  = f"https://www.ipcinfo.org/api/index.cfm?action=getCountryAnalysis&country={iso3}&format=json"
                data = _http_json(url, timeout=20)
                # latest period
                periods = data if isinstance(data, list) else (data.get("periods") or data.get("data") or [])
                if not periods:
                    continue
                latest = periods[-1] if isinstance(periods, list) else periods
                p3 = (
                    latest.get("p3plus") or latest.get("phase3plus")
                    or latest.get("total_phase3plus") or latest.get("affected_population")
                )
                if p3 and float(p3) > 0:
                    ipc[country] = int(float(p3) / 1000)
            except Exception:
                pass

    return ipc


# ── ACLED — conflict casualties (last 12 months) ──────────────────────────
def fetch_acled_casualties(crisis_countries: List[str]) -> Dict[str, int]:
    """
    Return {canonical_country: conflict_fatalities_thousands}.
    Requires ACLED_API_KEY and ACLED_EMAIL environment variables
    (set as GitHub Secrets: ACLED_API_KEY, ACLED_EMAIL).
    Silently returns {} if credentials are absent.
    """
    api_key = os.environ.get("ACLED_API_KEY", "").strip()
    email   = os.environ.get("ACLED_EMAIL", "").strip()
    if not api_key or not email:
        return {}

    casualties: Dict[str, int] = {}
    since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    today = dt.date.today().isoformat()

    # Query per country to keep responses manageable
    for country in crisis_countries:
        try:
            params = urllib.parse.urlencode({
                "key":              api_key,
                "email":            email,
                "country":          country,
                "event_date":       f"{since}|{today}",
                "event_date_where": "BETWEEN",
                "fields":           "fatalities",
                "limit":            0,
            })
            url  = f"https://api.acleddata.com/acled/read?{params}"
            data = _http_json(url, timeout=30)
            total = sum(int(ev.get("fatalities") or 0) for ev in (data.get("data") or []))
            if total > 0:
                casualties[country] = int(total / 1000)
        except Exception as e:
            print(f"  [ACLED/{country}] {type(e).__name__}: {e}", file=sys.stderr)

    return casualties


# ── WHO — disease outbreak risk (1-5) ────────────────────────────────────
def fetch_who_disease_risk() -> Dict[str, int]:
    """
    Return {canonical_country: disease_risk_score_1_to_5}.
    Uses WHO Disease Outbreak News RSS/JSON feed — no key required.
    Scores: 1 active outbreak = +1, capped at 5.
    """
    risk: Dict[str, int] = {}
    try:
        data  = _http_json(
            "https://www.who.int/api/news/diseaseoutbreaknews?sf_culture=en&$top=200",
            timeout=30,
        )
        items = data.get("value") or data.get("items") or data.get("data") or []
        counts: Dict[str, int] = {}
        for item in items:
            # WHO DON entries usually have a 'title' like "Cholera – Sudan"
            title = item.get("Title") or item.get("title") or ""
            # Try to find country in title
            for country, _ in COUNTRY_ISO3.items():
                if country.lower() in title.lower():
                    counts[country] = counts.get(country, 0) + 1
                    break
        for country, count in counts.items():
            risk[country] = min(5, max(1, count))
    except Exception as e:
        print(f"  [WHO] {type(e).__name__}: {e}", file=sys.stderr)

    return risk


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
            p       = f.get("properties", f) or {}
            alert   = (p.get("alertlevel") or "").lower()
            sev     = 5 if alert == "red" else 4
            etype   = p.get("eventtype", "")
            sub     = DISASTER_SUBTYPES.get(etype, "Disaster")
            raw_c   = (p.get("country") or "").strip()
            country = _norm(raw_c) if raw_c else None
            if not country:
                continue
            out.append({
                "country":  country,
                "subType":  sub,
                "severity": sev,
                "alert":    alert,
                "name":     p.get("eventname") or sub,
                "id":       f"GDACS-{p.get('eventid','?')}",
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
            f        = d.get("fields", {}) or {}
            countries = f.get("country") or []
            raw      = (countries[0].get("name") if countries else None) \
                       or (f.get("primary_country") or {}).get("name") or ""
            country  = _norm(raw)
            if not country or country == "Unknown":
                continue
            types = f.get("type") or []
            ctype = (types[0].get("name") if types else None) or "Disaster"
            out.append({
                "country": country,
                "type":    ctype,
                "name":    f.get("name") or ctype,
            })
        return out
    except Exception as e:
        print(f"  [ReliefWeb] {type(e).__name__}: {e}", file=sys.stderr)
        return []


# ── Main update logic ─────────────────────────────────────────────────────
def update_crises() -> None:
    print(f"[{TODAY}] update_crises.py starting…")

    c_fields, crises     = _read_csv(CRISES_CSV)
    i_fields, indicators = _read_csv(INDICATORS_CSV)

    crises_by_id      = {r["Crisis ID"]: r for r in crises}
    crises_by_country = {r["Country"].lower(): r for r in crises}
    indic_by_id       = {r["Crisis ID"]: r for r in indicators}

    changes = 0

    # ── 1. FTS: Appeal Funded (%) + PIN ───────────────────────────────────
    print("  Fetching FTS funding + PIN data…", end=" ")
    fts_funded, fts_pin = fetch_fts_data()
    print(f"{len(fts_funded)} plans, {len(fts_pin)} PIN entries")

    for crisis in crises:
        country = crisis["Country"]
        cid     = crisis["Crisis ID"]
        indic   = indic_by_id.get(cid)
        if not indic:
            continue

        # Helper: find best match across funded/pin dicts
        def _match(d):
            val = d.get(country)
            if val is None:
                for k, v in d.items():
                    if k.lower() in country.lower() or country.lower() in k.lower():
                        val = v
                        break
            return val

        # Appeal Funded (%)
        pct = _match(fts_funded)
        if pct is not None:
            old = indic.get("Appeal Funded (%)", "")
            if _meaningful_change(old, pct):
                indic["Appeal Funded (%)"] = str(pct)
                crisis["Last Updated"] = TODAY
                changes += 1
                print(f"    FTS funded%: {country} {old} → {pct}")

        # People in Need (PIN '000)
        pin_val = _match(fts_pin)
        if pin_val is not None:
            col = "People in Need (PIN '000)"
            old = indic.get(col, "")
            if _meaningful_change(old, pin_val):
                indic[col] = str(pin_val)
                crisis["Last Updated"] = TODAY
                changes += 1
                print(f"    FTS PIN:    {country} {old} → {pin_val}k")

    # ── 2. UNHCR: Displaced (IDPs + Refugees '000) ────────────────────────
    print("  Fetching UNHCR displacement data…", end=" ")
    unhcr = fetch_unhcr_displacement()
    print(f"{len(unhcr)} countries")

    for crisis in crises:
        country = crisis["Country"]
        cid     = crisis["Crisis ID"]
        indic   = indic_by_id.get(cid)
        if not indic:
            continue
        val = unhcr.get(country)
        if val is None:
            for k, v in unhcr.items():
                if k.lower() in country.lower() or country.lower() in k.lower():
                    val = v
                    break
        if val is None:
            continue
        col = "Displaced (IDPs+Refugees '000)"
        old = indic.get(col, "")
        if _meaningful_change(old, val):
            indic[col] = str(val)
            crisis["Last Updated"] = TODAY
            changes += 1
            print(f"    UNHCR displaced: {country} {old} → {val}k")

    # ── 3. IPC: Phase 3+ ('000) ───────────────────────────────────────────
    print("  Fetching IPC food-security phases…", end=" ")
    ipc = fetch_ipc_phases()
    print(f"{len(ipc)} countries")

    for crisis in crises:
        country = crisis["Country"]
        cid     = crisis["Crisis ID"]
        indic   = indic_by_id.get(cid)
        if not indic:
            continue
        val = ipc.get(country)
        if val is None:
            continue
        col = "IPC Phase 3+ ('000)"
        old = indic.get(col, "")
        if _meaningful_change(old, val):
            indic[col] = str(val)
            crisis["Last Updated"] = TODAY
            changes += 1
            print(f"    IPC Phase3+: {country} {old} → {val}k")

    # ── 4. ACLED: Casualties ('000) — optional ────────────────────────────
    print("  Fetching ACLED casualties…", end=" ")
    crisis_countries = [c["Country"] for c in crises]
    acled = fetch_acled_casualties(crisis_countries)
    if acled:
        print(f"{len(acled)} countries")
        for crisis in crises:
            country = crisis["Country"]
            cid     = crisis["Crisis ID"]
            indic   = indic_by_id.get(cid)
            if not indic:
                continue
            val = acled.get(country)
            if val is None:
                continue
            col = "Casualties ('000)"
            old = indic.get(col, "")
            if _meaningful_change(old, val):
                indic[col] = str(val)
                crisis["Last Updated"] = TODAY
                changes += 1
                print(f"    ACLED casualties: {country} {old} → {val}k")
    else:
        print("skipped (no ACLED_API_KEY set)")

    # ── 5. WHO: Disease Outbreak Risk (1-5) ───────────────────────────────
    print("  Fetching WHO disease outbreak data…", end=" ")
    who = fetch_who_disease_risk()
    print(f"{len(who)} countries with active outbreaks")

    for crisis in crises:
        country = crisis["Country"]
        cid     = crisis["Crisis ID"]
        indic   = indic_by_id.get(cid)
        if not indic:
            continue
        val = who.get(country)
        if val is None:
            continue
        col = "Disease Outbreak Risk (1-5)"
        old = indic.get(col, "")
        try:
            old_i = int(old)
        except (ValueError, TypeError):
            old_i = None
        if old_i != val:
            indic[col] = str(val)
            crisis["Last Updated"] = TODAY
            changes += 1
            print(f"    WHO disease risk: {country} {old} → {val}")

    # ── 6. GDACS: Disaster severity + new crises ──────────────────────────
    print("  Fetching GDACS events…", end=" ")
    gdacs_events = fetch_gdacs_events()
    print(f"{len(gdacs_events)} alerts")

    new_gdacs_countries: set = set()
    for ev in gdacs_events:
        country = ev["country"]
        new_gdacs_countries.add(country.lower())
        existing = crises_by_country.get(country.lower())
        if existing:
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
            if ev["severity"] >= NEW_CRISIS_MIN_SEVERITY:
                new_id = _next_crisis_id(crises)
                print(f"    GDACS new crisis: {country} ({ev['subType']}, sev={ev['severity']}) → {new_id} [NEEDS REVIEW]")
                new_crisis = {f: "" for f in c_fields}
                new_crisis.update({
                    "Crisis ID":             new_id,
                    "Country":               country,
                    "Region":                "",
                    "Crisis Type":           "Disaster",
                    "Status":                "Active",
                    "Start Date":            TODAY,
                    "Severity (1-5)":        str(ev["severity"]),
                    "INFORM Risk":           "",
                    "Source":                "GDACS",
                    "Last Updated":          TODAY,
                    "Notes / Trigger Event": f"REVIEW NEEDED — {ev['subType']} ({ev['alert'].upper()} alert). Auto-detected by update_crises.py.",
                })
                crises.append(new_crisis)
                crises_by_country[country.lower()] = new_crisis

                new_indic = {f: "" for f in i_fields}
                new_indic.update({"Crisis ID": new_id, "Country": country})
                indicators.append(new_indic)
                indic_by_id[new_id] = new_indic
                changes += 1

    # ── 7. ReliefWeb: new crises (double-source check) ────────────────────
    print("  Fetching ReliefWeb events…", end=" ")
    rw_events = fetch_reliefweb_events()
    print(f"{len(rw_events)} events")

    seen_rw: set = set()
    for ev in rw_events:
        country = ev["country"]
        key     = country.lower()
        if key in seen_rw or key in crises_by_country:
            seen_rw.add(key)
            continue
        seen_rw.add(key)
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

    # ── 8. Write back ──────────────────────────────────────────────────────
    if changes:
        _write_csv(CRISES_CSV,     c_fields, crises)
        _write_csv(INDICATORS_CSV, i_fields, indicators)
        print(f"  ✓ {changes} change(s) written to CSVs.")
    else:
        print("  ✓ No changes — CSVs already up to date.")

    print("update_crises.py done.")


if __name__ == "__main__":
    update_crises()
