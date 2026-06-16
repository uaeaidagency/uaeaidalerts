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


import xml.etree.ElementTree as _ET
import email.utils as _eu
import re as _re


def _iso3_lower(country: str) -> str:
    """Best-effort ISO3 code for a country name, used in ReliefWeb URLs."""
    try:
        import world_countries as _wc
        info = _wc.describe(country)
        if info and info.get("iso3"):
            return info["iso3"].lower()
    except Exception:
        pass
    # tiny inline fallback for the most common cases
    fb = {"philippines":"phl","sudan":"sdn","yemen":"yem","syria":"syr",
          "syrian arab republic":"syr","ukraine":"ukr","gaza":"pse",
          "palestine":"pse","afghanistan":"afg","myanmar":"mmr","somalia":"som",
          "ethiopia":"eth","democratic republic of the congo":"cod","dr congo":"cod",
          "haiti":"hti","lebanon":"lbn","mali":"mli","burkina faso":"bfa",
          "niger":"ner","nigeria":"nga","chad":"tcd","cameroon":"cmr",
          "central african republic":"caf","libya":"lby","iraq":"irq",
          "pakistan":"pak","bangladesh":"bgd","türkiye":"tur","turkey":"tur",
          "indonesia":"idn","sri lanka":"lka","mozambique":"moz",
          "kenya":"ken","zimbabwe":"zwe","malawi":"mwi","venezuela":"ven",
          "south sudan":"ssd","japan":"jpn"}
    return fb.get((country or "").lower(), "")


def _parse_rss(xmltext: str, source_default: str = "ReliefWeb") -> List[dict]:
    """Parse an RSS 2.0 feed. Returns list of {title, source, url, date}."""
    out: List[dict] = []
    if not xmltext:
        return out
    try:
        root = _ET.fromstring(xmltext)
    except Exception:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link") or "").strip()
        pub   = (it.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        iso = ""
        if pub:
            try:
                iso = _eu.parsedate_to_datetime(pub).strftime("%Y-%m-%d")
            except Exception:
                iso = pub[:10]
        out.append({"title": title, "source": source_default, "url": link, "date": iso})
    return out


# ── ReliefWeb: current disasters for a country (RSS — JSON API now requires
#    a pre-approved appname and returns 410 to ours). ─────────────────────────
def reliefweb_disasters(country: str, limit: int = 5) -> List[dict]:
    iso3 = _iso3_lower(country)
    # Country page RSS — ReliefWeb publishes all recent updates here.
    urls = []
    if iso3:
        urls.append(f"https://reliefweb.int/country/{iso3}/rss.xml")
    urls.append("https://reliefweb.int/updates/rss.xml")   # global fallback

    items: List[dict] = []
    for u in urls:
        items = _parse_rss(_http_text(u, timeout=6))
        if items:
            break

    # Detect a "current disaster" by the title pattern. ReliefWeb titles
    # often include the disaster type, e.g. "Philippines: M 7.8 Earthquake".
    DISASTER_KW = [
        ("Earthquake","earthquake"), ("Tsunami","tsunami"),
        ("Typhoon","typhoon"), ("Cyclone","cyclone"), ("Hurricane","hurricane"),
        ("Storm","tropical storm"), ("Flood","flood"), ("Floods","floods"),
        ("Drought","drought"), ("Wildfire","wildfire"), ("Volcano","volcano"),
        ("Landslide","landslide"), ("Cholera","cholera"), ("Outbreak","outbreak"),
        ("Conflict","conflict"), ("Displacement","displacement"),
        ("Famine","famine"), ("Mpox","mpox"), ("Measles","measles"),
    ]
    cl = (country or "").lower()
    out: List[dict] = []
    for it in items:
        title = it["title"]
        tl = title.lower()
        if cl and cl not in tl:
            continue
        type_label = "Humanitarian update"
        for label, kw in DISASTER_KW:
            if kw in tl:
                type_label = label
                break
        out.append({
            "name": title,
            "type": type_label,
            "status": "current",
            "url": it["url"],
            "date": it["date"],
        })
        if len(out) >= limit:
            break
    return out


# ── ReliefWeb: latest situation reports / news for a country (RSS). ────────
def reliefweb_reports(country: str, limit: int = 5) -> List[dict]:
    iso3 = _iso3_lower(country)
    url = f"https://reliefweb.int/country/{iso3}/rss.xml" if iso3 \
          else "https://reliefweb.int/updates/rss.xml"
    items = _parse_rss(_http_text(url, timeout=6))
    cl = (country or "").lower()
    out: List[dict] = []
    for it in items:
        if cl and cl not in it["title"].lower():
            continue
        out.append({
            "title": it["title"],
            "source": it["source"],
            "url": it["url"],
            "date": it["date"],
        })
        if len(out) >= limit:
            break
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
