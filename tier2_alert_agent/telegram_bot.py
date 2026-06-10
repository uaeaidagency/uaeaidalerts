"""
UAE Aid Agency — Tier-2+ Alert Agent
Telegram listener bot.

Long-polls the Telegram Bot API for incoming messages. When a recognized
country name is mentioned, replies with a crisis overview text and
attaches the country's executive summary PDF.

Open to all users by default. The chat_ids in config.json are used only
for outbound alert broadcasts (run_check.py), not to restrict inbound queries.

Usage:
    python telegram_bot.py             # run in current shell
    python telegram_bot.py --once       # process pending updates, then exit

Commands the bot understands:
    /start    /help    — show usage
    /list     /crises  — list all crises by current tier
    Sudan              — country name (free-form or with /update)
    /update Yemen      — explicit update command
    What's happening in Lebanon?  — natural language works too

DEPENDENCIES: Standard library only (urllib, json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)          # repo root — where the CSVs live
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Optional: GitHub raw base URL for pulling live CSVs on cloud deployments.
# Set as an environment variable, e.g.:
#   GITHUB_RAW_BASE=https://raw.githubusercontent.com/your-org/your-repo/main
GITHUB_RAW_BASE = os.environ.get("GITHUB_RAW_BASE", "").rstrip("/")

from scoring import compute_all_scores, CrisisScore  # noqa: E402
from telegram_sender import _read_token, _send_message, _send_document  # noqa: E402

try:
    from pdf_export import export_country_pdf
    _DASHBOARD_PDF_AVAILABLE = True
except Exception:
    _DASHBOARD_PDF_AVAILABLE = False

from exec_summary import generate_pdf as generate_reportlab_pdf  # noqa: E402

try:
    import world_countries
    _WORLD_COUNTRIES_AVAILABLE = True
except Exception:
    _WORLD_COUNTRIES_AVAILABLE = False

try:
    import live_crises
    _LIVE_CRISES_AVAILABLE = True
except Exception:
    _LIVE_CRISES_AVAILABLE = False

try:
    import news_digest
    _NEWS_DIGEST_AVAILABLE = True
except Exception:
    _NEWS_DIGEST_AVAILABLE = False

CONFIG_FILE = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
STATE_DIR = os.path.join(HERE, "state")
OUTPUT_DIR = os.path.join(HERE, "output")
OFFSET_FILE = os.path.join(STATE_DIR, "telegram_offset.json")

POLL_TIMEOUT_S = 25
RETRY_BACKOFF_S = 5
SCORES_REFRESH_S = 300   # recompute scores every 5 minutes
RUN_CHECK_INTERVAL_S = 3600  # run tier-alert check every hour

# Common aliases / informal names → canonical country name in STATE.crises.
COUNTRY_ALIASES = {
    "drc": "DR Congo",
    "congo": "DR Congo",
    "dr congo": "DR Congo",
    "democratic republic": "DR Congo",
    "gaza": "Palestine (Gaza)",
    "palestine": "Palestine (Gaza)",
    "west bank": "Palestine (Gaza)",
    "turkey": "Türkiye",
    "turkiye": "Türkiye",
    "türkiye": "Türkiye",
    "south sudan": "South Sudan",
    "burkina": "Burkina Faso",
    "burkina faso": "Burkina Faso",
    "sri lanka": "Sri Lanka",
    "ivory coast": "Côte d'Ivoire",
}


CSV_FILES = [
    "01_Active_Crises.csv",
    "02_Humanitarian_Indicators.csv",
    "03_UAE_Priority_Countries.csv",
    "04_Response_Decision_Log.csv",
]


def _refresh_csvs() -> None:
    """Pull the latest CSVs from GitHub raw URLs (cloud deployments only).
    When GITHUB_RAW_BASE is not set this is a no-op — local files are used."""
    if not GITHUB_RAW_BASE:
        return
    for filename in CSV_FILES:
        url = f"{GITHUB_RAW_BASE}/{filename}"
        local = os.path.join(PARENT, filename)
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            with open(local, "wb") as f:
                f.write(data)
        except Exception as e:
            _log(f"CSV refresh failed for {filename}: {e}")


def _log(message: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"bot_{dt.date.today().isoformat()}.log")
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_offset() -> int:
    if not os.path.exists(OFFSET_FILE):
        return 0
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset, "updated_at": dt.datetime.now().isoformat()}, f)


def _drain_backlog(token: str) -> int:
    """Skip ALL pending updates on startup so the bot never replies to a
    backlog of old messages. Returns the offset to resume from (one past
    the most recent pending update). Uses timeout=0 so it returns at once.

    This is the permanent fix for the 'bot replied to 199 old messages'
    problem: on every startup we acknowledge the whole queue without
    handling any of it, then only process messages that arrive afterward.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    next_offset = 0
    try:
        # offset=-1 returns just the last pending update (if any).
        params = urllib.parse.urlencode({"offset": -1, "timeout": 0})
        with urllib.request.urlopen(f"{url}?{params}", timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("result") or []
        if results:
            last_id = int(results[-1]["update_id"])
            next_offset = last_id + 1
            # Confirm/clear everything up to and including last_id.
            confirm = urllib.parse.urlencode({"offset": next_offset, "timeout": 0})
            with urllib.request.urlopen(f"{url}?{confirm}", timeout=15) as resp:
                json.loads(resp.read().decode("utf-8"))
            _log(f"Drained backlog up to update {last_id}; resuming from {next_offset}.")
        else:
            _log("No backlog to drain.")
    except Exception as e:
        _log(f"Backlog drain failed (continuing anyway): {e}")
    return next_offset


def _get_updates(token: str, offset: int) -> dict:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = urllib.parse.urlencode({
        "offset": offset,
        "timeout": POLL_TIMEOUT_S,
        "allowed_updates": json.dumps(["message"]),
    })
    try:
        with urllib.request.urlopen(f"{url}?{params}",
                                     timeout=POLL_TIMEOUT_S + 10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        _log(f"getUpdates network error: {e}")
        return {"ok": False, "result": []}
    except Exception as e:
        _log(f"getUpdates exception: {e}")
        return {"ok": False, "result": []}


# ── Country resolution ──────────────────────────────────────────────────

def _resolve_country(text: str, countries: List[str]) -> Optional[str]:
    """Find the country mentioned in `text`. Returns the canonical name or None."""
    norm = text.lower().strip()

    # Aliases first (handles "DRC", "gaza", etc.)
    for alias, canonical in COUNTRY_ALIASES.items():
        if alias in norm and canonical in countries:
            return canonical

    # Substring match (longest country name wins to avoid "Sudan" matching "South Sudan")
    matches = [c for c in countries if c.lower() in norm]
    if matches:
        matches.sort(key=len, reverse=True)
        return matches[0]

    # Word-level fuzzy fallback
    text_words = set(norm.replace("(", " ").replace(")", " ").split())
    for c in countries:
        c_words = set(c.lower().replace("(", " ").replace(")", " ").split())
        # All country words must appear in the message words
        if c_words and c_words.issubset(text_words):
            return c

    return None


# ── Message formatting ──────────────────────────────────────────────────

_TIER_EMOJI = {1: "🟥", 2: "🟧", 3: "🟨", 4: "⬜", 5: "⬛"}
_TIER_LABEL = {
    1: "TIER 1 — IMMEDIATE RESPONSE",
    2: "TIER 2 — STRONG RESPONSE",
    3: "TIER 3 — TARGETED",
    4: "TIER 4 — MONITOR",
    5: "TIER 5 — TRACK ONLY",
}
_TIER_ACTION = {
    1: "Mobilize ERT; pledge within 7 days; brief DG within 24 hours.",
    2: "Pledge within 14 days; multi-sector contribution via UN / IFRC / INGO.",
    3: "Single-sector contribution via pooled funds or trusted partner.",
    4: "Diplomatic engagement; earmarked contribution if formally requested.",
    5: "Monitor and track only; weekly reassessment.",
}


def _fmt_k(value_in_thousands: float) -> str:
    """Number is already in thousands. Returns e.g. '12.7M' or '450K'."""
    if not value_in_thousands or value_in_thousands <= 0:
        return "—"
    if value_in_thousands >= 1000:
        return f"{value_in_thousands/1000:.1f}M"
    return f"{value_in_thousands:,.0f}K"


def _escape_html(text: str) -> str:
    """Telegram HTML parse mode escapes."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_overview(score: CrisisScore) -> str:
    emoji = _TIER_EMOJI.get(score.decision_tier, "⬜")
    label = _TIER_LABEL.get(score.decision_tier, f"TIER {score.decision_tier}")
    action = _TIER_ACTION.get(score.decision_tier, "Review")
    trigger = _escape_html(score.trigger_event or "—")
    if len(trigger) > 350:
        trigger = trigger[:347] + "…"

    return (
        f"{emoji} <b>{_escape_html(score.country)}</b> — {label}\n"
        f"<i>Priority Score: <b>{score.priority_score:.1f}/100</b> · "
        f"ACAPS Severity: <b>{score.severity}/5</b></i>\n\n"
        f"• People in Need: <b>{_fmt_k(score.pin_thousands)}</b>\n"
        f"• Displaced (IDPs + refugees): {_fmt_k(score.displaced_thousands)}\n"
        f"• Casualties (confirmed): {_fmt_k(score.casualties_thousands)}\n"
        f"• IPC Phase 3+: {_fmt_k(score.ipc_phase3_thousands)}\n"
        f"• Children with SAM: {_fmt_k(score.children_malnourished_thousands)}\n"
        f"• Health facilities damaged: {score.health_facilities_damaged_pct:.0f}%\n"
        f"• Appeal funded: {score.appeal_funded_pct:.0f}%\n"
        f"• Access constraints: {score.access_constraints}/5\n\n"
        f"<b>Trigger:</b> {trigger}\n"
        f"<b>Status:</b> {_escape_html(score.status)} · "
        f"Updated {_escape_html(score.last_updated)}\n\n"
        f"<b>Recommended action:</b> {_escape_html(action)}\n\n"
        f"📎 Generating executive summary PDF…"
    )


def _help_text(countries: List[str]) -> str:
    return (
        "👋 <b>UAE Aid Agency — Monitoring Bot</b>\n\n"
        "Send <b>any country name</b> — works for every country on Earth:\n"
        "• Curated crisis → full scored overview + executive summary PDF.\n"
        "• Any other country → live status pulled in real time from "
        "<b>OCHA ReliefWeb</b> + <b>GDACS</b>, plus the latest news headlines "
        "from BBC, Reuters, AP, Al Jazeera, WAM, The National and other "
        "reputable outlets — with UAE priority status and engagement history.\n\n"
        "<b>Try:</b>\n"
        "• <code>Sudan</code> (curated crisis + PDF)\n"
        "• <code>Chad</code> (live crisis + news)\n"
        "• <code>Japan</code> (profile + any live alerts)\n"
        "• <code>What's happening in Lebanon?</code>\n\n"
        "<b>Commands:</b>\n"
        "<code>news now</code> — latest 5 world humanitarian headlines\n"
        "<code>/list</code> — every curated crisis grouped by tier\n"
        "<code>/help</code> — show this message\n\n"
        f"<i>{len(countries)} curated crises · every country on Earth recognised "
        "with live worldwide data.</i>"
    )


def _list_text(scores: List[CrisisScore]) -> str:
    by_tier: Dict[int, List[CrisisScore]] = {}
    for s in scores:
        by_tier.setdefault(s.decision_tier, []).append(s)

    lines = ["<b>Active crises by decision tier</b>"]
    tier_names = {
        1: "Tier 1 — Immediate",
        2: "Tier 2 — Strong",
        3: "Tier 3 — Targeted",
        4: "Tier 4 — Monitor",
        5: "Tier 5 — Track only",
    }
    for tier in sorted(by_tier.keys()):
        emoji = _TIER_EMOJI.get(tier, "⬜")
        title = tier_names.get(tier, f"Tier {tier}")
        lines.append(f"\n{emoji} <b>{title}</b>")
        for s in sorted(by_tier[tier], key=lambda x: -x.priority_score):
            lines.append(f"   • {_escape_html(s.country)} "
                           f"<i>({s.priority_score:.0f})</i>")
    return "\n".join(lines)


# ── Message handler ─────────────────────────────────────────────────────

def _try_generate_pdf(country: str) -> Optional[str]:
    """Try Edge-headless dashboard PDF first; fall back to reportlab."""
    if _DASHBOARD_PDF_AVAILABLE:
        try:
            return export_country_pdf(country, OUTPUT_DIR, lang="en", timeout_sec=45)
        except Exception as e:
            _log(f"Dashboard PDF failed for {country}: {e}; falling back to reportlab.")
    # Fallback: use reportlab to render a portrait brief.
    try:
        scores = compute_all_scores()
        match = next((s for s in scores if s.country == country), None)
        if not match:
            return None
        return generate_reportlab_pdf(match, previous_tier=None, output_dir=OUTPUT_DIR)
    except Exception as e:
        _log(f"reportlab fallback failed for {country}: {e}")
        return None


def _priority_status(country: str) -> Optional[str]:
    """Return the UAE strategic priority tier for a country (from
    03_UAE_Priority_Countries.csv), or None if not listed."""
    import csv
    path = os.path.join(PARENT, "03_UAE_Priority_Countries.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                name = (r.get("Country") or "").strip()
                if name.lower() == country.lower() or country.lower() in name.lower():
                    tier = (r.get("Tier (1-3)") or "").strip()
                    rationale = (r.get("Rationale") or "").strip()
                    return f"Tier {tier}" + (f" — {rationale}" if rationale else "")
    except Exception:
        pass
    return None


def _past_engagements(country: str) -> List[dict]:
    """Return past UAE response-log entries for a country (from
    04_Response_Decision_Log.csv), most recent first."""
    import csv
    path = os.path.join(PARENT, "04_Response_Decision_Log.csv")
    out: List[dict] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                name = (r.get("Country") or "").strip()
                if not name:
                    continue
                if name.lower() == country.lower() or country.lower() in name.lower() \
                        or name.lower() in country.lower():
                    out.append({
                        "date": (r.get("Date") or "").strip(),
                        "tier": (r.get("Tier") or "").strip(),
                        "decision": (r.get("Decision") or "").strip(),
                        "modality": (r.get("Modality") or "").strip(),
                        "amount": (r.get("Amount (USD '000)") or "").strip(),
                        "partner": (r.get("Lead Partner") or "").strip(),
                    })
    except Exception:
        pass
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _format_no_crisis(info: dict, country_name: str, snap: Optional[dict] = None) -> str:
    """Build the reply for a recognized country that is NOT one of the curated
    monitored crises. Pulls a LIVE snapshot (ReliefWeb disasters, GDACS alerts,
    recent reputable news) so the bot reports real current events for ANY
    country on Earth — not just the 25 curated crises."""
    name = info.get("name") or country_name
    region = info.get("region") or "—"
    capital = info.get("capital") or "—"
    population = info.get("population") or ""
    languages = info.get("languages") or ""

    meta_bits = [f"Region: {region}"]
    if capital and capital != "—":
        meta_bits.append(f"Capital: {capital}")
    if population:
        meta_bits.append(f"Population: {population}")
    meta_line = " · ".join(meta_bits)

    priority = _priority_status(name)
    engagements = _past_engagements(name)

    disasters = (snap or {}).get("disasters") or []
    alerts    = (snap or {}).get("alerts") or []
    news      = (snap or {}).get("news") or []
    has_active = bool(disasters or alerts)

    lines = [
        f"🌍 <b>{_escape_html(name)}</b>",
        f"<i>{_escape_html(meta_line)}</i>",
        "",
    ]

    # ── Live situation (from worldwide trusted sources) ──────────────────
    if has_active:
        lines.append("🔴 <b>Live situation — current events on record:</b>")
        for d in disasters[:4]:
            label = _escape_html(d.get("type") or "Disaster")
            nm = _escape_html(d.get("name") or name)
            url = d.get("url") or ""
            entry = f"   • <b>{label}</b> — {nm}"
            if url:
                entry += f"  <a href=\"{_escape_html(url)}\">[ReliefWeb]</a>"
            lines.append(entry)
        for a in alerts[:4]:
            lvl = (a.get("alert") or "").upper()
            label = _escape_html(a.get("type") or "Alert")
            nm = _escape_html(a.get("name") or "")
            url = a.get("url") or ""
            entry = f"   • <b>{label}</b> ({lvl}) — {nm}"
            if url:
                entry += f"  <a href=\"{_escape_html(url)}\">[GDACS]</a>"
            lines.append(entry)
        lines.append(
            "\n<i>Sources: OCHA ReliefWeb + GDACS. This country is tracked live "
            "but is not (yet) on the UAE curated scoring list.</i>"
        )
    else:
        lines.append(
            "✅ <b>No active humanitarian emergency detected</b> right now for "
            f"{_escape_html(name)} across OCHA ReliefWeb and GDACS live feeds."
        )

    # ── UAE priority + engagement history ────────────────────────────────
    if priority:
        lines.append(f"\n<b>UAE priority status:</b> {_escape_html(priority)}")
    else:
        lines.append("\n<b>UAE priority status:</b> Not on the strategic priority list.")

    if engagements:
        lines.append("<b>Prior UAE engagement on record:</b>")
        for e in engagements[:5]:
            amt = ""
            try:
                v = float(e["amount"]) * 1000 if e["amount"] else 0
                if v >= 1_000_000:
                    amt = f" · USD {v/1_000_000:.1f}M"
                elif v > 0:
                    amt = f" · USD {v/1000:.0f}K"
            except (ValueError, TypeError):
                pass
            lines.append(
                f"   • {_escape_html(e['date'])} — {_escape_html(e['decision'])} "
                f"({_escape_html(e['tier'])}){amt}"
                + (f" · {_escape_html(e['modality'])}" if e["modality"] else "")
            )
    else:
        lines.append("<b>Prior UAE engagement:</b> None on record.")

    # ── Recent news headlines (reputable outlets) ────────────────────────
    if news:
        lines.append("\n📰 <b>Latest news:</b>")
        for n in news[:5]:
            title = _escape_html((n.get("title") or "")[:140])
            url = n.get("url") or ""
            src = _escape_html(n.get("source") or "")
            date = _escape_html(n.get("date") or "")
            meta = " · ".join([b for b in (src, date) if b])
            if url:
                lines.append(f"   • <a href=\"{_escape_html(url)}\">{title}</a>"
                             + (f"  <i>{meta}</i>" if meta else ""))
            else:
                lines.append(f"   • {title}" + (f"  <i>{meta}</i>" if meta else ""))

    if languages:
        lines.append(f"\n<i>Official language(s): {_escape_html(languages)}</i>")

    return "\n".join(lines)


def _format_world_news(n: int = 5) -> str:
    """Build a Telegram reply summarising the top worldwide humanitarian /
    crisis stories. Each story shows the headline, a short overview, when it
    broke, which sources covered it, and how many sources reported it.
    Pulls live RSS + GDELT via news_digest (server-side, no CORS)."""
    clusters = []
    if _NEWS_DIGEST_AVAILABLE:
        try:
            clusters = news_digest.fetch_world_news_clusters(max_clusters=n)
        except Exception as e:
            _log(f"  world news fetch failed: {e}")
    if not clusters:
        return ("📰 <b>World News</b>\n\n"
                "Couldn't reach the news sources right now — please try again "
                "in a moment.")

    lines = ["📰 <b>UAE Aid — Latest World News</b>",
             f"<i>Top {len(clusters)} humanitarian/crisis stories, newest first</i>"]
    for i, c in enumerate(clusters, 1):
        title = _escape_html((c.get("title") or "")[:180])
        url = c.get("url") or ""
        head = f"<a href=\"{_escape_html(url)}\">{title}</a>" if url else title
        block = [f"\n<b>{i}. {head}</b>"]

        overview = (c.get("overview") or "").strip()
        if overview:
            block.append(_escape_html(overview[:260]))

        # Timing: when it broke (newest mention) + first-reported if different.
        rel = news_digest._rel_time(c.get("latest_ts") or 0)
        first = news_digest._rel_time(c.get("earliest_ts") or 0)
        timing = ""
        if rel:
            timing = f"🕒 {rel}"
            if first and first != rel:
                timing += f" · first reported {first}"
        if timing:
            block.append(timing)

        # Sources + how many covered it.
        sources = c.get("sources") or []
        count = c.get("count") or len(sources)
        if sources:
            shown = ", ".join(_escape_html(s) for s in sources[:5])
            extra = f" +{count - 5} more" if count > 5 else ""
            plural = "source" if count == 1 else "sources"
            block.append(f"📰 {count} {plural}: {shown}{extra}")

        lines.append("\n".join(block))
    return "\n".join(lines)


def _handle_message(token: str, message: dict, scores: List[CrisisScore],
                     authorized: Set[int]) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    if chat_id is None:
        return

    text = (message.get("text") or "").strip()
    sender = chat.get("first_name", "") + " " + (chat.get("last_name", "") or "")
    sender = sender.strip() or chat.get("username", "?")

    if not text:
        return

    # In GROUPS/SUPERGROUPS the bot must NOT reply to every message (that
    # floods active chats). Only respond when the message is a slash command
    # or explicitly @mentions the bot. In private 1:1 chats, respond to
    # everything. This is the key anti-spam guard.
    if chat_type in ("group", "supergroup"):
        tl = text.lower()
        is_command = tl.startswith("/")
        is_mention = ("@uaeaidbot" in tl) or ("@crisislensbot" in tl) or ("@uaeaid" in tl)
        if not (is_command or is_mention):
            return  # ignore ordinary group chatter silently
        # Strip the @mention so country resolution works on the rest.
        # Longest handle first so "@uaeaidbot" is removed before "@uaeaid".
        for handle in ("@uaeaidbot", "@UAEAIDBot", "@Crisislensbot",
                       "@crisislensbot", "@UAEAID", "@uaeaid"):
            text = text.replace(handle, "")
        text = text.strip()

    _log(f"Message from {sender} (chat {chat_id}, {chat_type}): {text[:120]}")

    text_lower = text.lower().strip()
    countries = [s.country for s in scores]

    # Built-in commands.
    if text_lower in ("/start", "/help", "help", "start", "hi", "hello"):
        _send_message(token, chat_id, _help_text(countries))
        return
    if text_lower in ("/list", "list", "/crises", "/countries"):
        _send_message(token, chat_id, _list_text(scores))
        return

    # World news command — "news now", "news live", "/news", etc.
    if text_lower in ("news now", "news live", "live news", "now news",
                      "/news", "news", "latest news", "world news"):
        _log("  → world news request")
        _send_message(token, chat_id, _format_world_news(5))
        return

    # Country lookup — first against the monitored crisis countries.
    country = _resolve_country(text, countries)
    if not country:
        # Not a curated crisis. Recognise it as ANY country on Earth and reply
        # with a LIVE snapshot (ReliefWeb disasters + GDACS alerts + news) plus
        # UAE history — so the bot works for every country, never "unknown".
        if _WORLD_COUNTRIES_AVAILABLE:
            try:
                info = world_countries.describe(text)
            except Exception as e:
                _log(f"  world_countries.describe failed: {e}")
                info = None
            if info:
                name = info.get("name", text)
                snap = None
                if _LIVE_CRISES_AVAILABLE:
                    try:
                        snap = live_crises.live_snapshot(name)
                    except Exception as e:
                        _log(f"  live_crises.live_snapshot failed: {e}")
                state = "LIVE CRISIS" if (snap and snap.get("has_active")) else "no active crisis"
                _log(f"  → {state} reply for {name}")
                _send_message(token, chat_id, _format_no_crisis(info, name, snap))
                return
        # Genuinely couldn't extract a country from the message.
        _send_message(
            token, chat_id,
            "🤔 I couldn't pick out a country from that message.\n\n"
            "Send a country name on its own — e.g. <code>Sudan</code>, "
            "<code>Chad</code>, <code>Japan</code>. Send <code>/list</code> "
            "to see the countries with active curated crises.",
        )
        return

    score = next((s for s in scores if s.country == country), None)
    if not score:
        _send_message(token, chat_id, f"❌ No data for {country}.")
        return

    # Send the overview first so the user gets an immediate response.
    _send_message(token, chat_id, _format_overview(score))

    # Then generate and send the PDF.
    pdf_path = _try_generate_pdf(country)
    if pdf_path and os.path.exists(pdf_path):
        try:
            _send_document(
                token, chat_id, pdf_path,
                caption=f"📎 <b>{_escape_html(country)}</b> — Executive Summary",
            )
            _log(f"  → Replied with overview + PDF for {country}")
        except Exception as e:
            _log(f"  ERROR sending PDF for {country}: {e}")
            _send_message(token, chat_id,
                           f"⚠️ PDF generated but failed to send: {e}")
    else:
        _send_message(token, chat_id,
                       "⚠️ Couldn't generate the executive summary PDF. "
                       "The overview above is current.")


# ── Background scheduler (run_check hourly) ────────────────────────────

def _run_check_loop() -> None:
    """Background thread: call run_check.run() once immediately, then every hour.

    Runs as a daemon so it doesn't prevent the process from exiting.
    Any exception inside run_check is caught and logged so the bot stays alive.
    """
    # Lazy import to avoid circular deps at module load time.
    try:
        import run_check
    except Exception as e:
        _log(f"[scheduler] Could not import run_check: {e}. Hourly checks disabled.")
        return

    # Delay the first run by 30 seconds so the bot has time to fully start up.
    time.sleep(30)
    while True:
        try:
            _log("[scheduler] Running hourly tier-alert check…")
            sent = run_check.run()
            _log(f"[scheduler] Hourly check complete. {sent} alert(s) sent.")
        except Exception as e:
            _log(f"[scheduler] run_check error: {e}\n{traceback.format_exc()}")
        time.sleep(RUN_CHECK_INTERVAL_S)


def _news_warm_loop() -> None:
    """Background thread: keep the world-news cache warm so 'news now' replies
    instantly instead of fetching all feeds on demand."""
    if not _NEWS_DIGEST_AVAILABLE:
        return
    while True:
        try:
            news_digest.fetch_world_news(limit=60)  # populates the shared cache
        except Exception as e:
            _log(f"[news] pre-warm failed: {e}")
        time.sleep(150)  # refresh just under the 180s cache TTL


# ── Main loop ───────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="UAE Aid Agency Telegram listener bot.")
    p.add_argument("--once", action="store_true",
                   help="Process pending updates and exit (no long-polling loop).")
    args = p.parse_args(argv)

    config = _load_config()
    tg_cfg = config.get("telegram") or {}
    if not tg_cfg.get("enabled"):
        _log("Telegram is disabled in config.json (telegram.enabled = false). Exiting.")
        return 1
    token = _read_token()
    if not token:
        _log("ERROR: TELEGRAM_BOT_TOKEN not set in telegram.env. Exiting.")
        return 1

    authorized = set(int(c) for c in (tg_cfg.get("chat_ids") or []) if c is not None)
    _log(f"Bot starting. Authorized chat_ids: {authorized or '(open to all)'}")

    # Always drain the pending backlog on startup so the bot never replies
    # to a flood of old messages (e.g. after a token change or downtime).
    drained_offset = _drain_backlog(token)
    saved_offset = _load_offset()
    offset = max(drained_offset, saved_offset)
    _save_offset(offset)
    _log(f"Starting from update offset {offset} (backlog skipped).")

    # Start the hourly run_check scheduler in a background daemon thread.
    # This fires tier-change emails + Telegram alerts automatically so the
    # Railway deployment doesn't need a separate cron process.
    if not args.once:
        scheduler = threading.Thread(target=_run_check_loop, daemon=True, name="run-check-scheduler")
        scheduler.start()
        _log("Hourly alert scheduler started (background thread).")

        # Keep the world-news cache warm so 'news now' replies instantly.
        if _NEWS_DIGEST_AVAILABLE:
            warmer = threading.Thread(target=_news_warm_loop, daemon=True, name="news-warmer")
            warmer.start()
            _log("World-news pre-warmer started (background thread).")

    scores: List[CrisisScore] = compute_all_scores()
    scores_at = time.time()

    try:
        while True:
            # Periodically pull fresh CSVs then recompute scores.
            if time.time() - scores_at > SCORES_REFRESH_S:
                try:
                    _refresh_csvs()
                    scores = compute_all_scores()
                    scores_at = time.time()
                    _log(f"Scores refreshed: {len(scores)} crises.")
                except Exception as e:
                    _log(f"Score refresh failed: {e}")

            resp = _get_updates(token, offset)
            if not resp.get("ok"):
                _log("getUpdates returned not-ok; backing off.")
                time.sleep(RETRY_BACKOFF_S)
                if args.once:
                    return 0
                continue

            updates = resp.get("result") or []
            for upd in updates:
                offset = max(offset, int(upd["update_id"]) + 1)
                message = upd.get("message")
                if message:
                    try:
                        _handle_message(token, message, scores, authorized)
                    except Exception as e:
                        _log(f"Handler error: {e}\n{traceback.format_exc()}")

            if updates:
                _save_offset(offset)

            if args.once:
                _log(f"--once: processed {len(updates)} update(s). Exiting.")
                return 0

    except KeyboardInterrupt:
        _log("Shutting down on Ctrl+C.")
        return 0
    except Exception as e:
        _log(f"Fatal error: {e}\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
