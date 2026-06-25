"""
UAE Aid Agency — Tier-2+ Alert Agent
Live event detector.

The curated CSV scoring engine (`scoring.py`) only sees the manually-maintained
crisis list. That means a brand-new major natural disaster in a country NOT on
the curated list (e.g. Venezuela twin earthquakes, a Cyclone in Madagascar)
never triggers a tier-change email — the Python pipeline simply doesn't know.

This module closes that gap. On every run it pulls:
  • GDACS event list (Red & Orange alerts) — global multi-hazard alerts
  • ReliefWeb /updates RSS — current humanitarian situations
…and reports any country with a major active event as an "emergency event"
that warrants an immediate Tier-1 style alert, regardless of what the
curated CSV says.

State is persisted to `state/seen_live_events.json` so the same earthquake
doesn't generate a fresh alert every 30 minutes — only NEW events trigger.

DEPENDENCIES: Standard library only. Safe to fail silently (best-effort).
"""
from __future__ import annotations

import datetime as dt
import email.utils as _eu
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as _ET
from typing import Dict, List, Optional, Set

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
SEEN_FILE = os.path.join(STATE_DIR, "seen_live_events.json")
APP = "uae-aid-monitor"

# Disaster types we treat as Tier-1 worthy when they are active right now.
_MAJOR_TYPES = {
    "TC": "Tropical Cyclone",
    "EQ": "Earthquake",
    "FL": "Flood",
    "DR": "Drought",
    "VO": "Volcanic Activity",
    "WF": "Wildfire",
}


def _http_text(url: str, timeout: int = 8) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "*/*",
            "User-Agent": f"{APP}/1.0 (UAE Aid Agency)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _http_json(url: str, timeout: int = 8) -> Optional[dict]:
    txt = _http_text(url, timeout=timeout)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _load_seen() -> Set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("event_ids", []))
    except Exception:
        return set()


def _save_seen(seen: Set[str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    # Cap at last 500 to keep the file from growing forever.
    items = list(seen)[-500:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "event_ids": items,
        }, f, indent=2)


# ── GDACS: live multi-hazard alerts globally ─────────────────────────────
def _fetch_gdacs() -> List[dict]:
    """Pull current Red/Orange GDACS alerts globally. Returns a normalized list."""
    url = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS?alertlevel=Red,Orange"
    j = _http_json(url)
    if not j:
        return []
    out: List[dict] = []
    feats = j.get("features") if isinstance(j, dict) else (j or [])
    for ftr in (feats or []):
        p = ftr.get("properties", ftr) if isinstance(ftr, dict) else {}
        etype = p.get("eventtype", "")
        eid   = str(p.get("eventid") or "")
        ename = p.get("eventname") or _MAJOR_TYPES.get(etype, "Disaster")
        country = (p.get("country") or "").split(",")[0].strip()
        alert = (p.get("alertlevel") or "").lower()
        date  = p.get("fromdate", "")
        url_  = ""
        u = p.get("url")
        if isinstance(u, dict):
            url_ = u.get("report") or u.get("details") or ""
        magnitude = ""
        if etype == "EQ":
            magnitude = str(p.get("severitydata", {}).get("severity", "") if isinstance(p.get("severitydata"), dict) else "")
        out.append({
            "source": "GDACS",
            "id": f"gdacs:{etype}:{eid}",
            "etype": etype,
            "type_label": _MAJOR_TYPES.get(etype, "Disaster"),
            "name": ename,
            "country": country,
            "alert": alert,
            "date": date,
            "url": url_,
            "magnitude": magnitude,
        })
    return out


# ── ReliefWeb: current disasters from the RSS feed (JSON API gates appname). ─
def _fetch_reliefweb_current() -> List[dict]:
    url = "https://reliefweb.int/updates/rss.xml"
    text = _http_text(url)
    if not text:
        return []
    out: List[dict] = []
    try:
        root = _ET.fromstring(text)
    except Exception:
        return out
    DISASTER_KW = [
        ("Earthquake", "earthquake"), ("Cyclone", "cyclone"),
        ("Typhoon", "typhoon"), ("Hurricane", "hurricane"),
        ("Tsunami", "tsunami"), ("Volcano", "volcan"),
        ("Floods", "flood"), ("Drought", "drought"),
        ("Wildfire", "wildfire"), ("Mpox", "mpox"),
        ("Cholera", "cholera"),
    ]
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link") or "").strip()
        pub   = (it.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        tl = title.lower()
        # Try to extract country from "Country: rest" or "Country - rest"
        country = ""
        for sep in [":", " - ", " – "]:
            if sep in title:
                country = title.split(sep, 1)[0].strip()
                if 1 < len(country) < 50:
                    break
        type_label = ""
        for label, kw in DISASTER_KW:
            if kw in tl:
                type_label = label
                break
        if not type_label:
            continue
        iso = ""
        if pub:
            try:
                iso = _eu.parsedate_to_datetime(pub).strftime("%Y-%m-%d")
            except Exception:
                iso = pub[:10]
        out.append({
            "source": "ReliefWeb",
            "id": f"rw:{link}",
            "etype": "",
            "type_label": type_label,
            "name": title,
            "country": country,
            "alert": "current",
            "date": iso,
            "url": link,
            "magnitude": "",
        })
    return out


def detect_new_major_events() -> List[dict]:
    """Return major live events whose IDs are not in the seen-set yet.
    Caller is responsible for calling `mark_seen()` AFTER a successful alert
    so failed deliveries get retried on the next run."""
    seen = _load_seen()
    all_events: Dict[str, dict] = {}

    # Prefer GDACS (richest data) but fall back to ReliefWeb if GDACS empty.
    for ev in _fetch_gdacs():
        all_events[ev["id"]] = ev
    for ev in _fetch_reliefweb_current():
        # Keep ReliefWeb only if we don't already have GDACS coverage of the
        # same country+type — avoids duplicate alerts for the same disaster.
        keep = True
        for existing in all_events.values():
            if (existing.get("country", "").lower() == ev.get("country", "").lower()
                    and existing.get("type_label") == ev.get("type_label")):
                keep = False
                break
        if keep:
            all_events[ev["id"]] = ev

    new_events = [ev for eid, ev in all_events.items() if eid not in seen]
    # Sort by alert level priority (Red > Orange > current), then date desc.
    rank = {"red": 0, "orange": 1, "current": 2}
    new_events.sort(key=lambda e: (rank.get(e.get("alert", "current"), 9), -1*len(e.get("date", ""))))
    return new_events


def mark_seen(events: List[dict]) -> None:
    seen = _load_seen()
    for ev in events:
        if ev.get("id"):
            seen.add(ev["id"])
    _save_seen(seen)


if __name__ == "__main__":
    evs = detect_new_major_events()
    print(f"Detected {len(evs)} new major live event(s):")
    for ev in evs:
        print(f"  [{ev['alert']:6}] {ev['country']:30} {ev['type_label']:25} {ev['name']}")
