"""
UAE Aid Agency — Tier-2+ Alert Agent
Telegram listener bot.

Long-polls the Telegram Bot API for incoming messages. When a recognized
country name is mentioned, replies with a crisis overview text and
attaches the country's executive summary PDF.

Restricted by default to the chat_ids configured in config.json
("telegram": {"chat_ids": [...]}). Unauthorized chats receive a polite
denial — and the bot logs the attempt so you can decide whether to
whitelist them.

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

CONFIG_FILE = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
STATE_DIR = os.path.join(HERE, "state")
OUTPUT_DIR = os.path.join(HERE, "output")
OFFSET_FILE = os.path.join(STATE_DIR, "telegram_offset.json")

POLL_TIMEOUT_S = 25
RETRY_BACKOFF_S = 5
SCORES_REFRESH_S = 300  # recompute scores every 5 minutes

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
        "Send a country name and I'll reply with the current crisis overview "
        "and attach the executive summary PDF.\n\n"
        "<b>Try:</b>\n"
        "• <code>Sudan</code>\n"
        "• <code>/update Yemen</code>\n"
        "• <code>What's happening in Lebanon?</code>\n\n"
        "<b>Commands:</b>\n"
        "<code>/list</code> — every crisis grouped by tier\n"
        "<code>/help</code> — show this message\n\n"
        f"<i>{len(countries)} countries currently monitored.</i>"
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


def _handle_message(token: str, message: dict, scores: List[CrisisScore],
                     authorized: Set[int]) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = (message.get("text") or "").strip()
    sender = chat.get("first_name", "") + " " + (chat.get("last_name", "") or "")
    sender = sender.strip() or chat.get("username", "?")
    _log(f"Message from {sender} (chat {chat_id}): {text[:120]}")

    # Security gate — skipped when query_open=true in config.json.
    if not _load_config().get("query_open", False):
        if authorized and chat_id not in authorized:
            _log(f"  → IGNORED (chat_id {chat_id} not in whitelist {authorized})")
            _send_message(
                token, chat_id,
                "🔒 This bot is restricted to authorized UAE Aid Agency contacts. "
                f"Forward your chat ID (<code>{chat_id}</code>) to the administrator "
                "to be whitelisted.",
            )
            return

    if not text:
        return

    text_lower = text.lower().strip()
    countries = [s.country for s in scores]

    # Built-in commands.
    if text_lower in ("/start", "/help", "help", "start", "hi", "hello"):
        _send_message(token, chat_id, _help_text(countries))
        return
    if text_lower in ("/list", "list", "/crises", "/countries"):
        _send_message(token, chat_id, _list_text(scores))
        return

    # Country lookup.
    country = _resolve_country(text, countries)
    if not country:
        _send_message(
            token, chat_id,
            "🤔 I couldn't find a country in your message.\n\n"
            "Try a country name on its own — e.g. <code>Sudan</code>, "
            "<code>DR Congo</code>, <code>Lebanon</code>. Or send "
            "<code>/list</code> to see every monitored country.",
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

    offset = _load_offset()
    _log(f"Resuming from update offset {offset}.")

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
