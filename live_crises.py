"""
UAE Aid Agency — Tier-2+ Alert Agent
Live worldwide crisis & news layer.

Lets the Telegram bot report REAL, CURRENT crises and news for ANY country
on Earth — not only the curated CSV crises. Pulls from public, free,
no-API-key, CORS/edge-friendly sources:

  • ReliefWeb (OCHA)  — current disasters + situation reports per country
  • GDACS             — live multi-hazard alerts (cyclone, flood, drought…)
  • GDELT DOC API     — recent news headlines from reputable global outlets

All network calls are best-effort with short timeouts; if a source is
unreachable the others still work and the bot degrades gracefully.

DEPENDENCIES: Standard library only (urllib, json).
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

APP = "uae-aid-monitor"
_CACHE: Dict[str, dict] = {}
_CACHE_TTL = 15 * 60  # seconds

# Reputable outlets we surface from GDELT (drop everything else).
_REPUTABLE = (
    "bbc.com bbc.co.uk reuters.com apnews.com aljazeera.com aljazeera.net "
    "theguardian.com nytimes.com washingtonpost.com ft.com economist.com cnn.com "
    "france24.com dw.com npr.org bloomberg.com euronews.com "
    "wam.ae thenationalnews.com khaleejtimes.com gulfnews.com arabnews.com "
    "aawsat.com alarabiya.net middleeasteye.net al-monitor.com aa.com.tr "
    "africanews.com allafrica.com theafricareport.com sudantribune.com "
    "thediplomat.com nikkei.com scmp.com dawn.com thehindu.com "
    "thenewhumanitarian.org devex.com reliefweb.int news.un.org foreignpolicy.com "
    "crisisgroup.org chathamhouse.org cfr.org"
).split()


def _http_json(url: str, timeout: int = 6) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": f"{APP}/1.0 (UAE Aid Agency)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _http_text(url: str, timeout: int = 6) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "*/*",
            "User-Agent": f"{APP}/1.0 (UAE Aid Agency)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _reputable(domain: str) -> bool:
    if not domain:
        return False
    d = domain.lower()
    return any(d == r or d.endswith("." + r) for r in _REPUTABLE)


# ── ReliefWeb: current disasters for a country ─────────────────────────────
def reliefweb_disasters(country: str, limit: int = 5) -> List[dict]:
    enc = urllib.parse.quote(country)
    url = (
        "https://api.reliefweb.int/v1/disasters"
        f"?appname={APP}&limit={limit}&profile=list"
        f"&filter[operator]=AND"
        f"&filter[conditions][0][field]=country.name&filter[conditions][0][value]={enc}"
        f"&filter[conditions][1][field]=status&filter[conditions][1][value]=current"
        "&sort[]=date.created:desc"
    )
    j = _http_json(url)
    out: List[dict] = []
    if not j:
        return out
    for d in (j.get("data") or []):
        f = d.get("fields", {}) or {}
        types = f.get("type") or []
        out.append({
            "name": f.get("name") or country,
            "type": (types[0].get("name") if types else "Disaster"),
            "status": f.get("status", "current"),
            "url": f.get("url") or "",
            "date": (f.get("date") or {}).get("created", ""),
        })
    return out


# ── ReliefWeb: latest situation reports / news for a country ───────────────
def reliefweb_reports(country: str, limit: int = 5) -> List[dict]:
    enc = urllib.parse.quote(country)
    url = (
        "https://api.reliefweb.int/v1/reports"
        f"?appname={APP}&limit={limit}&profile=list"
        f"&query[value]={enc}&query[fields][]=country.name"
        "&sort[]=date.created:desc"
        "&fields[include][]=title&fields[include][]=source"
        "&fields[include][]=url&fields[include][]=date"
    )
    j = _http_json(url)
    out: List[dict] = []
    if not j:
        return out
    for d in (j.get("data") or []):
        f = d.get("fields", {}) or {}
        src = (f.get("source") or [{}])
        out.append({
            "title": f.get("title") or "",
            "source": (src[0].get("name") if src else "ReliefWeb"),
            "url": f.get("url") or "",
            "date": (f.get("date") or {}).get("created", ""),
        })
    return out


# ── GDACS: live multi-hazard alerts, filtered to a country ─────────────────
def gdacs_alerts(country: str, limit: int = 5) -> List[dict]:
    url = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS?alertlevel=Red,Orange"
    j = _http_json(url, timeout=6)
    if not j:
        return []
    feats = j.get("features") if isinstance(j, dict) else (j or [])
    type_map = {"TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake",
                "DR": "Drought", "VO": "Volcano", "WF": "Wildfire"}
    cl = country.lower()
    out: List[dict] = []
    for ftr in (feats or []):
        p = ftr.get("properties", ftr) if isinstance(ftr, dict) else {}
        c = (p.get("country") or "")
        ev = (p.get("eventname") or "")
        if cl not in c.lower() and cl not in ev.lower():
            continue
        out.append({
            "name": ev or type_map.get(p.get("eventtype"), "Disaster"),
            "type": type_map.get(p.get("eventtype"), "Disaster"),
            "alert": (p.get("alertlevel") or "").lower(),
            "date": p.get("fromdate", ""),
            "url": (p.get("url") or {}).get("report", "") if isinstance(p.get("url"), dict) else "",
        })
        if len(out) >= limit:
            break
    return out


# ── GDELT: recent news headlines from reputable outlets ────────────────────
def gdelt_news(country: str, limit: int = 6) -> List[dict]:
    q = f'("{country}") (humanitarian OR conflict OR crisis OR refugees OR displacement OR aid OR disaster OR flood OR earthquake OR drought)'
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc?query="
        + urllib.parse.quote(q)
        + "&mode=ArtList&format=json&maxrecords=40&timespan=21d&sort=DateDesc"
    )
    txt = _http_text(url, timeout=6)
    out: List[dict] = []
    if not txt:
        return out
    try:
        j = json.loads(txt)
    except Exception:
        return out
    for a in (j.get("articles") or []):
        if not a.get("url") or not _reputable(a.get("domain", "")):
            continue
        iso = ""
        s = a.get("seendate", "")
        if len(s) >= 8:
            iso = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        out.append({
            "title": a.get("title") or "",
            "source": a.get("domain", ""),
            "url": a.get("url"),
            "date": iso,
        })
        if len(out) >= limit:
            break
    return out


# ── Aggregate: full live picture for a country ─────────────────────────────
def live_snapshot(country: str) -> dict:
    """Return {disasters, reports, alerts, news, has_active} for a country.
    Cached 15 min. All sources best-effort."""
    key = country.lower().strip()
    now = dt.datetime.utcnow().timestamp()
    cached = _CACHE.get(key)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    # Fire all four sources at once instead of one-after-another, so the
    # total wait is the SLOWEST single source (~a few seconds) rather than
    # the SUM of all four. Each task is best-effort; a failure returns [].
    from concurrent.futures import ThreadPoolExecutor

    def _safe(fn):
        try:
            return fn(country)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_disasters = ex.submit(_safe, reliefweb_disasters)
        f_alerts    = ex.submit(_safe, gdacs_alerts)
        f_reports   = ex.submit(_safe, reliefweb_reports)
        f_news      = ex.submit(_safe, gdelt_news)
        disasters = f_disasters.result()
        alerts    = f_alerts.result()
        reports   = f_reports.result()
        news      = f_news.result()

    data = {
        "country": country,
        "disasters": disasters,
        "alerts": alerts,
        "reports": reports,
        "news": news,
        # "Active" if any authoritative humanitarian source flags it.
        "has_active": bool(disasters or alerts),
    }
    _CACHE[key] = {"ts": now, "data": data}
    return data


if __name__ == "__main__":
    import sys
    c = " ".join(sys.argv[1:]) or "Chad"
    snap = live_snapshot(c)
    print(json.dumps(snap, indent=2, ensure_ascii=False)[:4000])
