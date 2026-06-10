"""
UAE Aid Agency — Tier-2+ Alert Agent
World News Digest.

When a detection run finds NO tier transitions (the usual case when the
situation is stable), the agent still sends leadership a snapshot of the
latest worldwide humanitarian/crisis news so the 30-minute cadence always
delivers something useful — not silence.

Sources (public, free, no API key, pulled server-side so there are no
browser/CORS limits):
  • GDELT DOC API  — newest headlines worldwide from reputable outlets
  • ReliefWeb (OCHA) — latest situation reports / updates globally

Both are best-effort with short timeouts; if one is unreachable the other
still produces a digest. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import List, Optional

APP = "uae-aid-monitor"
HERE = os.path.dirname(os.path.abspath(__file__))

# Diagnostics: every failed fetch records its reason here so `python
# news_digest.py` (or the --news-digest run) can explain WHY 0 headlines came
# back instead of failing silently.
_LAST_ERRORS: List[str] = []

# Reputable outlets we surface from GDELT — global wires + Emirati/Gulf +
# UN/humanitarian. Anything outside this list is dropped as noise.
_REPUTABLE = (
    "bbc.com bbc.co.uk reuters.com apnews.com afp.com aljazeera.com aljazeera.net "
    "theguardian.com nytimes.com washingtonpost.com wsj.com ft.com economist.com "
    "cnn.com france24.com dw.com npr.org bloomberg.com euronews.com news.sky.com "
    "abcnews.go.com cbsnews.com nbcnews.com cbc.ca abc.net.au time.com "
    "wam.ae thenationalnews.com khaleejtimes.com gulfnews.com gulftoday.ae "
    "albayan.ae alittihad.ae aletihad.ae emaratalyoum.com alroeya.com alkhaleej.ae "
    "arabnews.com saudigazette.com.sa spa.gov.sa aawsat.com alarabiya.net "
    "gulf-times.com thepeninsulaqatar.com qna.org.qa omanobserver.om timesofoman.com "
    "kuwaittimes.com kuna.net.kw bna.bh menafn.com zawya.com "
    "middleeasteye.net al-monitor.com aa.com.tr africanews.com allafrica.com "
    "sudantribune.com thediplomat.com scmp.com dawn.com thehindu.com "
    "thenewhumanitarian.org devex.com reliefweb.int news.un.org unhcr.org wfp.org "
    "unicef.org icrc.org msf.org foreignpolicy.com crisisgroup.org"
).split()


def _http_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch text over HTTPS. Tries normal certificate verification first; if
    that fails (common behind TLS-intercepting corporate/government proxies),
    retries once with verification relaxed. Records the reason on failure."""
    req = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "User-Agent": f"{APP}/1.0 (UAE Aid Agency)",
    })
    contexts = [None]  # default verified context
    try:
        contexts.append(ssl._create_unverified_context())
    except Exception:
        pass
    last_err = None
    for ctx in contexts:
        try:
            kwargs = {"timeout": timeout}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last_err = e
    _LAST_ERRORS.append(f"{url.split('?')[0]} -> {type(last_err).__name__}: {last_err}")
    return None


def _reputable(domain: str) -> bool:
    if not domain:
        return False
    d = domain.lower()
    return any(d == r or d.endswith("." + r) for r in _REPUTABLE)


def _source_name(domain: str) -> str:
    labels = {
        "wam.ae": "WAM (Emirates News Agency)", "thenationalnews.com": "The National",
        "khaleejtimes.com": "Khaleej Times", "gulfnews.com": "Gulf News",
        "alittihad.ae": "Al Ittihad", "aletihad.ae": "Aletihad",
        "emaratalyoum.com": "Emarat Al Youm", "alarabiya.net": "Al Arabiya",
        "aljazeera.com": "Al Jazeera", "reuters.com": "Reuters",
        "apnews.com": "Associated Press", "bbc.com": "BBC News", "bbc.co.uk": "BBC News",
        "reliefweb.int": "ReliefWeb (OCHA)", "news.un.org": "UN News",
        "arabnews.com": "Arab News", "aawsat.com": "Asharq Al-Awsat",
    }
    d = (domain or "").lower().replace("www.", "")
    return labels.get(d, d)


# Public RSS feeds — no API key, no appname approval, very reliable from a PC.
# (url, source label, filter_by_crisis_keywords)
_RSS_FEEDS = [
    ("https://reliefweb.int/updates/rss.xml",                         "ReliefWeb (OCHA)",       False),
    ("https://news.un.org/feed/subscribe/en/news/all/rss.xml",        "UN News",                False),
    ("https://www.thenewhumanitarian.org/rss.xml",                    "The New Humanitarian",   False),
    ("https://www.aljazeera.com/xml/rss/all.xml",                     "Al Jazeera",             True),
    ("https://feeds.bbci.co.uk/news/world/rss.xml",                   "BBC News",               True),
    ("https://www.khaleejtimes.com/rss",                              "Khaleej Times",          True),
]

_CRISIS_KW = (
    "humanitarian", "refugee", "displace", "famine", "flood", "earthquake",
    "cyclone", "hurricane", "typhoon", "drought", "wildfire", "conflict", "war",
    "airstrike", "evacuat", "outbreak", "cholera", " aid", "crisis", "disaster",
    "relief", "quake", "storm", "violence", "killed", "attack",
)


def _clean_html(s: str, limit: int = 240) -> str:
    """Strip tags/whitespace from an RSS description and truncate to one blurb."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def _parse_rss(xmltext: str, source: str, filt: bool) -> List[dict]:
    items: List[dict] = []
    try:
        root = ET.fromstring(xmltext)
    except Exception:
        return items
    # RSS 2.0 <item> elements (works for all feeds above).
    nodes = list(root.iter("item"))
    # Atom fallback (<entry>) for feeds that use it.
    if not nodes:
        nodes = [e for e in root.iter() if e.tag.endswith("}entry") or e.tag == "entry"]
    for it in nodes:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:  # Atom stores the URL in <link href="...">
            for ch in it:
                if ch.tag.endswith("link") and ch.get("href"):
                    link = ch.get("href").strip()
                    break
        pub = (it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
        desc = (it.findtext("description")
                or it.findtext("summary")
                or it.findtext("{http://www.w3.org/2005/Atom}summary")
                or "")
        if not title or not link:
            continue
        if filt and not any(k in title.lower() for k in _CRISIS_KW):
            continue
        iso, ts = "", 0.0
        if pub:
            try:
                d = email.utils.parsedate_to_datetime(pub)
                iso = d.strftime("%Y-%m-%d %H:%M")
                ts = d.timestamp()
            except Exception:
                iso = pub[:16]
        items.append({"title": title, "source": source, "url": link,
                      "date": iso, "ts": ts, "overview": _clean_html(desc), "country": ""})
    return items


# Short in-process cache so repeated requests (e.g. several "news now" messages)
# return instantly instead of re-fetching every feed each time.
_NEWS_CACHE = {"ts": 0.0, "items": []}
_NEWS_CACHE_TTL = 180  # seconds


def _fetch_one_rss(feed) -> List[dict]:
    url, source, filt = feed
    txt = _http_text(url, timeout=8)
    return _parse_rss(txt, source, filt) if txt else []


def _fetch_gdelt() -> List[dict]:
    out: List[dict] = []
    q = ('(humanitarian OR refugees OR displacement OR famine OR flood OR '
         'earthquake OR cyclone OR drought OR conflict OR outbreak)')
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query="
           + urllib.parse.quote(q)
           + "&mode=ArtList&format=json&maxrecords=75&timespan=36h&sort=DateDesc")
    txt = _http_text(url, timeout=8)
    if not txt:
        return out
    try:
        j = json.loads(txt)
    except Exception:
        _LAST_ERRORS.append("GDELT returned non-JSON (likely a rate-limit or query message).")
        return out
    for a in (j.get("articles") or []):
        u = a.get("url")
        if not u or not _reputable(a.get("domain", "")):
            continue
        iso, ts = "", 0.0
        s = a.get("seendate", "")
        if len(s) >= 8:
            iso = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10] or '00'}:{s[10:12] or '00'}"
            try:
                ts = dt.datetime(int(s[:4]), int(s[4:6]), int(s[6:8]),
                                 int(s[8:10] or 0), int(s[10:12] or 0),
                                 tzinfo=dt.timezone.utc).timestamp()
            except Exception:
                ts = 0.0
        out.append({"title": a.get("title") or "", "source": _source_name(a.get("domain", "")),
                    "url": u, "date": iso, "ts": ts, "overview": "", "country": ""})
    return out


def fetch_world_news(limit: int = 24, use_cache: bool = True) -> List[dict]:
    """Newest worldwide humanitarian/crisis headlines, newest first.
    All sources (RSS feeds + GDELT) are fetched IN PARALLEL so the total wait
    is the slowest single source, not the sum. Cached for a few minutes."""
    now = dt.datetime.now().timestamp()
    if use_cache and _NEWS_CACHE["items"] and (now - _NEWS_CACHE["ts"]) < _NEWS_CACHE_TTL:
        return _NEWS_CACHE["items"][:limit]

    from concurrent.futures import ThreadPoolExecutor

    out: List[dict] = []
    seen = set()
    tasks = []
    with ThreadPoolExecutor(max_workers=len(_RSS_FEEDS) + 1) as ex:
        for feed in _RSS_FEEDS:
            tasks.append(ex.submit(_fetch_one_rss, feed))
        gdelt_future = ex.submit(_fetch_gdelt)
        results = [t.result() for t in tasks]
        try:
            results.append(gdelt_future.result())
        except Exception:
            pass

    for lst in results:
        for it in lst:
            u = it.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(it)

    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    if out:
        _NEWS_CACHE["ts"] = now
        _NEWS_CACHE["items"] = out
    return out[:limit]


def _rel_time(ts: float) -> str:
    """'just now' / '12m ago' / '3h ago' / '2d ago' from an epoch timestamp."""
    if not ts:
        return ""
    diff = dt.datetime.now(dt.timezone.utc).timestamp() - ts
    if diff < 0:
        diff = 0
    m = int(diff // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


_STOPWORDS = set((
    "the a an of in on at to for and or with from by as is are was were be been "
    "being this that these those over under amid into out new news latest say says "
    "said after before during world more than two one its their his her them they "
    "has have had will would could about against amid up down off live update"
).split())


def _sig_words(title: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (title or "").lower())
            if w not in _STOPWORDS}


def cluster_news(items: List[dict], max_clusters: int = 5) -> List[dict]:
    """Group headlines that describe the same story (shared significant words)
    across sources. Returns clusters newest-first, each with a representative
    headline + overview, the distinct sources, how many covered it, and timing."""
    clusters: List[dict] = []
    for it in items:
        w = _sig_words(it.get("title", ""))
        best, best_score = None, 0.0
        for c in clusters:
            inter = len(w & c["words"])
            union = len(w | c["words"]) or 1
            jac = inter / union
            if inter >= 2 and jac > best_score:
                best, best_score = c, jac
        if best and best_score >= 0.18:
            best["items"].append(it)
            best["words"] |= w
        else:
            clusters.append({"words": set(w), "items": [it]})

    out: List[dict] = []
    for c in clusters:
        its = c["items"]
        rep = max(its, key=lambda x: (len(x.get("overview") or ""), len(x.get("title") or "")))
        sources, source_seen = [], set()
        for x in its:
            s = (x.get("source") or "").strip()
            if s and s.lower() not in source_seen:
                source_seen.add(s.lower())
                sources.append(s)
        tss = [x.get("ts") for x in its if x.get("ts")]
        overview = rep.get("overview") or ""
        if not overview:
            for x in its:
                if x.get("overview"):
                    overview = x["overview"]
                    break
        out.append({
            "title": rep.get("title", ""),
            "overview": overview,
            "url": rep.get("url", ""),
            "sources": sources,
            "count": len(sources),
            "latest_ts": max(tss) if tss else 0.0,
            "earliest_ts": min(tss) if tss else 0.0,
            "date": rep.get("date", ""),
        })

    out.sort(key=lambda c: c["latest_ts"], reverse=True)
    return out[:max_clusters]


def fetch_world_news_clusters(max_clusters: int = 5, pool: int = 60) -> List[dict]:
    """Fetch a wide pool of headlines then cluster them into the top stories."""
    return cluster_news(fetch_world_news(limit=pool), max_clusters=max_clusters)


def _build_email_html(items: List[dict], dashboard_button: Optional[str] = None) -> str:
    now = dt.datetime.now().strftime("%d %b %Y · %H:%M")
    rows = []
    for n in items:
        meta = n["source"]
        if n.get("country"):
            meta += f" · {n['country']}"
        if n.get("date"):
            meta += f" · {n['date']}"
        rows.append(
            f'<tr><td style="padding:9px 0;border-bottom:1px solid #EEF1F4;">'
            f'<a href="{n["url"]}" style="color:#1D252C;font-weight:600;font-size:13px;'
            f'text-decoration:none;">{n["title"]}</a>'
            f'<div style="font-size:11px;color:#6B7280;margin-top:2px;">{meta}</div>'
            f'</td></tr>'
        )
    body_rows = "".join(rows) or (
        '<tr><td style="padding:12px 0;color:#6B7280;font-size:13px;">'
        'No fresh headlines were retrieved this cycle. The next digest is in 30 minutes.'
        '</td></tr>'
    )
    return f"""\
<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1F2937;">
<table cellpadding="0" cellspacing="0" style="background:#1D252C;color:white;
       padding:14px 18px;border-radius:8px;border-top:3px solid #FBAE40;
       border-bottom:3px solid #FBAE40;width:100%;max-width:660px;">
  <tr><td>
    <div style="font-weight:700;letter-spacing:.5px;">UAE AID AGENCY</div>
    <div style="font-size:12px;color:#CBDCE6;">World News Digest · {now}</div>
  </td></tr>
</table>
<p style="font-size:13px;color:#374151;margin:16px 0 4px 0;">
  No crisis changed Decision Tier this cycle. Here is the latest worldwide
  humanitarian &amp; crisis news, newest first, from trusted global, Gulf and
  UN sources.
</p>
<table cellpadding="0" cellspacing="0" style="width:100%;max-width:660px;">
  {body_rows}
</table>
{dashboard_button or ""}
<p style="font-size:11px;color:#9CA3AF;margin-top:18px;border-top:1px solid #E5E7EB;padding-top:10px;">
  Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent (world news
  digest). Sources: GDELT global wire + ReliefWeb (OCHA). Sent every 30 minutes
  when no tier transition occurs.
</p>
</body>
</html>
"""


def _build_telegram(items: List[dict], dashboard_link: Optional[str] = None) -> str:
    now = dt.datetime.now().strftime("%d %b · %H:%M")
    lines = [f"🌐 <b>UAE AID AGENCY</b> — World News Digest\n<i>{now} · no tier change this cycle</i>\n"]
    for n in items[:12]:
        meta = n["source"]
        if n.get("country"):
            meta += f" · {n['country']}"
        lines.append(f'• <a href="{n["url"]}">{n["title"]}</a>\n  <i>{meta}</i>')
    body = "\n".join(lines)
    if dashboard_link:
        body += dashboard_link
    # Telegram message hard cap ~4096 chars.
    return body[:3900]


def _curated_fallback() -> List[dict]:
    """When the live news APIs are unreachable, build a digest from the agency's
    own current crisis picture so an email still goes out. Clearly an internal
    snapshot, not external news."""
    items: List[dict] = []
    try:
        from scoring import compute_all_scores
        scores = sorted(compute_all_scores(), key=lambda s: -s.priority_score)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for s in scores[:18]:
            trig = (s.trigger_event or s.crisis_type or "").strip()
            title = f"{s.country} — {s.crisis_type} (Tier {s.decision_tier}, severity {s.severity}/5)"
            if trig and trig.lower() not in title.lower():
                title += f": {trig}"
            items.append({"title": title, "source": "UAE Aid Monitoring (internal)",
                          "url": "", "date": now, "country": s.region})
    except Exception:
        pass
    return items


def send_world_news_digest(
    recipients,
    tg_chat_ids,
    tg_enabled,
    dashboard_button=None,
    dashboard_link=None,
    logger=print,
) -> bool:
    """Build and send the world-news digest via email + Telegram. Best-effort."""
    items = fetch_world_news()
    live = bool(items)
    if not items:
        for err in _LAST_ERRORS[-4:]:
            logger(f"  News digest: source unreachable — {err}")
        items = _curated_fallback()
        if items:
            logger(f"  News digest: live feeds unreachable; sending curated internal snapshot "
                   f"({len(items)} crises) so the email still goes out.")
        else:
            logger("  News digest: no live news AND no curated data — skipping send.")
            return False

    label = "headlines" if live else "crisis lines (curated — live feeds unreachable)"
    subject = f"[UAE Aid · World News Digest] {dt.datetime.now():%d %b %H:%M} — {len(items)} {label}"
    email_html = _build_email_html(items, dashboard_button=dashboard_button)
    tg_text = _build_telegram(items, dashboard_link=dashboard_link)

    # Email-only by design: a news roundup every 30 minutes would spam the
    # Telegram groups. Tier-change alerts still reach Telegram via the main flow.
    ok = False
    try:
        from emailer import send_alert
        if recipients:
            if send_alert(recipients, subject, email_html):
                logger(f"  News digest email sent to {recipients} ({len(items)} {label}).")
                ok = True
            else:
                logger("  ERROR: news digest email failed.")
        else:
            logger("  News digest: no email recipients configured — nothing to send.")
    except Exception as e:
        logger(f"  WARN: news digest email error: {e}")

    return ok


def write_dashboard_news(out_path: str, limit: int = 40) -> int:
    """Fetch live world news and write it as a JS file the dashboard loads
    locally (no browser CORS/relay needed). Returns the number of items written.
    Falls back to the curated internal snapshot if the live feeds are blocked."""
    items = fetch_world_news(limit=limit)
    live = bool(items)
    if not items:
        items = _curated_fallback()
    ts = dt.datetime.now().isoformat(timespec="seconds")
    payload = json.dumps(items, ensure_ascii=False)
    js = (
        "/* Auto-generated by news_digest.write_dashboard_news — do not edit. */\n"
        f"window.WORLD_NEWS_SEED = {payload};\n"
        f"window.WORLD_NEWS_SEED_TS = {json.dumps(ts)};\n"
        f"window.WORLD_NEWS_SEED_LIVE = {'true' if live else 'false'};\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    return len(items)


if __name__ == "__main__":
    import sys as _sys
    if "--write-dashboard" in _sys.argv:
        # Write world_news_data.js next to the dashboard (parent folder).
        target = os.path.join(os.path.dirname(HERE), "world_news_data.js")
        n = write_dashboard_news(target)
        print(f"Wrote {n} news items to {target}")
        _sys.exit(0)
    print("Testing live news fetch (GDELT + ReliefWeb)...\n")
    news = fetch_world_news()
    print(f"Fetched {len(news)} live headlines.\n")
    for n in news:
        print(f"- {n['title']}  [{n['source']}{(' · ' + n['country']) if n['country'] else ''}]")
    if _LAST_ERRORS:
        print("\n--- Fetch errors (why sources returned nothing) ---")
        for e in _LAST_ERRORS:
            print(f"  {e}")
    if not news:
        print("\nLive feeds unreachable. Falling back to curated internal snapshot:")
        for n in _curated_fallback():
            print(f"- {n['title']}")
