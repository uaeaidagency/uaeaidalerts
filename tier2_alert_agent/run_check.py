"""
UAE Aid Agency — Tier-2+ Alert Agent
Orchestrator entry point.

ALERT TRIGGER: any crisis whose Decision Tier is now 1 or 2 AND whose
previous tier was different (i.e. it newly entered the high-response
zone, or it moved between Tier 2 and Tier 1).

Each run:
  1. Loads `config.json` (recipients, options).
  2. Computes current Decision Tier for every active crisis (see scoring.py).
  3. Compares against `state/last_tiers.json` (the previous run's tiers).
  4. For every crisis newly at Tier 1 or Tier 2 (or moving between them):
        - generates an executive summary PDF (exec_summary.py),
        - emails it to the configured recipients (emailer.py).
  5. Writes a structured log entry to `logs/run_<date>.log`.
  6. Persists the new tier state.

First run notes:
  If `state/last_tiers.json` doesn't exist, the agent seeds it from the
  current tiers and sends NO alerts. This prevents an alert flood on
  initial deployment. Subsequent runs alert on transitions only.

USAGE:
    python run_check.py                   # one-shot check
    python run_check.py --force           # alert on every current Tier 1/2,
                                          #   regardless of previous state
                                          #   (useful for first-run testing)
    python run_check.py --dry-run         # compute + log only, no email
    python run_check.py --simulate <ID>=2 # pretend crisis ID has new tier 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from typing import Dict, List, Optional

# Allow this script to import sibling modules when run from any cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from scoring import CrisisScore, compute_all_scores, to_dict  # noqa: E402
from exec_summary import generate_pdf as generate_pdf_fallback  # noqa: E402
from emailer import send_alert  # noqa: E402

try:
    import telegram_sender  # noqa: E402
    _TELEGRAM_AVAILABLE = True
except Exception as _telegram_err:
    _TELEGRAM_AVAILABLE = False

import approval_watcher  # noqa: E402
import submission_watcher  # noqa: E402
import project_brief  # noqa: E402

# Dashboard-driven PDF export is the primary source; reportlab is the fallback.
try:
    from pdf_export import export_country_pdf  # noqa: E402
    _PDF_DASHBOARD_AVAILABLE = True
except Exception as _pdf_export_err:  # pragma: no cover
    _PDF_DASHBOARD_AVAILABLE = False
    _PDF_DASHBOARD_ERR = _pdf_export_err


def generate_pdf(score, previous_tier, output_dir):
    """Try the dashboard's brand-template PDF first; fall back to reportlab."""
    if _PDF_DASHBOARD_AVAILABLE:
        try:
            return export_country_pdf(score.country, output_dir, lang="en")
        except Exception as e:
            print(
                f"[run_check] dashboard PDF export failed for {score.country}: {e}; "
                f"falling back to reportlab generator.",
                file=sys.stderr,
            )
    return generate_pdf_fallback(score, previous_tier, output_dir)

CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_DIR = os.path.join(HERE, "state")
STATE_FILE = os.path.join(STATE_DIR, "last_tiers.json")
LOG_DIR = os.path.join(HERE, "logs")
OUTPUT_DIR = os.path.join(HERE, "output")


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_state() -> Optional[Dict[str, int]]:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: int(v) for k, v in data.get("tiers", {}).items()}


def _save_state(tiers: Dict[str, int]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": dt.datetime.now().isoformat(timespec="seconds"), "tiers": tiers},
            f,
            indent=2,
        )


def _log(message: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"run_{dt.date.today().isoformat()}.log")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _direction_phrase(previous_tier: Optional[int], current_tier: int) -> str:
    if previous_tier is None:
        return f"Newly classified at Tier {current_tier}"
    if previous_tier == current_tier:
        return f"Reconfirmed at Tier {current_tier}"
    if previous_tier > current_tier:
        return f"Escalation from Tier {previous_tier} → Tier {current_tier}"
    return f"De-escalation from Tier {previous_tier} → Tier {current_tier}"


def _tier_label(t: int) -> str:
    return {
        1: "TIER 1 — IMMEDIATE RESPONSE",
        2: "TIER 2 — STRONG RESPONSE",
    }.get(t, f"TIER {t}")


def _tier_action_line(t: int) -> str:
    if t == 1:
        return ("Per methodology §4.3, Tier 1 calls for mobilizing the Emergency Response Team, "
                "pledging within 7 days, and considering UAE Search &amp; Rescue / medical / "
                "aircraft mission deployment plus high-level political signalling.")
    return ("Per methodology §4.3, Tier 2 calls for pledging within 14 days, "
            "multi-sector contribution via UN agencies, IFRC, or trusted INGOs.")


def _dashboard_link(
    config: dict,
    label: str = "UAE Aid Agency — Live Humanitarian Monitoring Dashboard",
) -> tuple:
    """Return (email_html_block, telegram_text_block, plain_url) or (None, None, None).

    For http/https URLs we emit a real clickable button (email) and link (Telegram).
    For file:// (or any non-web scheme) we present the path as monospaced text
    next to a "Copy & paste into your browser" hint, since web clients block
    file:// hyperlinks.
    """
    url = (config.get("dashboard_url") or "").strip()
    if not url:
        return None, None, None

    is_web = url.lower().startswith(("http://", "https://", "tg://", "mailto:"))

    if is_web:
        email_block = (
            f'<p style="margin:18px 0 6px 0;">'
            f'<a href="{url}" '
            f'style="background:#1D252C;color:#FFFFFF;text-decoration:none;'
            f'padding:10px 18px;border-radius:6px;font-weight:600;'
            f'font-size:13px;display:inline-block;'
            f'border-top:2px solid #FBAE40;border-bottom:2px solid #FBAE40;">'
            f'{label} →</a></p>'
        )
        tg_block = f'\n🔗 <a href="{url}">{label}</a>'
    else:
        # file:// presentation: monospaced URL + copy-paste hint.
        email_block = (
            f'<div style="margin:18px 0 6px 0;padding:12px 14px;'
            f'background:#F4F7FA;border-left:4px solid #FBAE40;border-radius:6px;">'
            f'<div style="font-size:11px;color:#6B7280;letter-spacing:.5px;'
            f'text-transform:uppercase;margin-bottom:4px;">'
            f'{label}</div>'
            f'<div style="font-family:Consolas,Menlo,monospace;font-size:11px;'
            f'color:#1D252C;word-break:break-all;">{url}</div>'
            f'<div style="font-size:11px;color:#6B7280;margin-top:6px;">'
            f'Copy &amp; paste this path into your browser (Gmail web blocks '
            f'<code>file://</code> hyperlinks for security).</div>'
            f'</div>'
        )
        tg_block = (
            f'\n🔗 <b>{label}</b>\n'
            f'<code>{url}</code>\n'
            f'<i>(long-press to copy &amp; open in your browser)</i>'
        )
    return email_block, tg_block, url


def _fmt_money_usd(usd: float) -> str:
    if usd <= 0:
        return "—"
    if usd >= 1_000_000:
        return f"USD {usd/1_000_000:,.1f} M"
    if usd >= 1_000:
        return f"USD {usd/1_000:,.0f} K"
    return f"USD {usd:,.0f}"


def _format_submission_email_subject(s: "submission_watcher.Submission") -> str:
    return (
        f"[UAE Aid · NEW SUBMISSION — Action Needed] "
        f"{s.country} — {s.project_name} — {_fmt_money_usd(s.proposed_amount_usd)}"
    )


def _format_submission_email_body(
    s: "submission_watcher.Submission",
    pdf_path: str,
    dashboard_button: Optional[str] = None,
) -> str:
    return f"""\
<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1F2937;">
<table cellpadding="0" cellspacing="0" style="background:#1D252C;color:white;
       padding:14px 18px;border-radius:8px;border-top:3px solid #FBAE40;
       border-bottom:3px solid #FBAE40;width:100%;max-width:640px;">
  <tr><td>
    <div style="font-weight:700;letter-spacing:.5px;">UAE AID AGENCY</div>
    <div style="font-size:12px;color:#CBDCE6;">New Project Submission — Decision Required</div>
  </td></tr>
</table>
<p style="font-size:16px;margin:18px 0 6px 0;">
  A new project has been <b style="color:#E07A00;">submitted for sign-off</b>.
</p>
<p style="font-size:13px;color:#374151;margin:0 0 14px 0;">
  {s.submission_id} · Submitted {s.submitted_date} by {s.submitted_by or "—"}
</p>
<table cellpadding="6" style="border-collapse:collapse;font-size:13px;">
  <tr><td style="color:#6B7280;">Project</td><td><b>{s.project_name}</b></td></tr>
  <tr><td style="color:#6B7280;">Country</td><td><b>{s.country}</b> (Tier {s.tier})</td></tr>
  <tr><td style="color:#6B7280;">Crisis Location</td><td>{s.location or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Trigger</td><td>{s.trigger or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Proposed Modality</td><td>{s.proposed_modality or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Proposed Amount</td><td><b>{_fmt_money_usd(s.proposed_amount_usd)}</b></td></tr>
  <tr><td style="color:#6B7280;">Implementer</td><td>{s.implementer or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Duration</td><td>{s.duration or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Point of Contact</td><td>{s.point_of_contact or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Approver</td><td><b>{s.approver or "—"}</b></td></tr>
  <tr><td style="color:#6B7280;">Notes</td><td>{s.notes or "—"}</td></tr>
</table>
<p style="font-size:13px;margin-top:14px;">
  <b>Action required:</b> APPROVE, DEFER, or REJECT. Open the dashboard
  → Approvals tab → Submission <code>{s.submission_id}</code> to record
  the decision. Country executive summary attached for context.
</p>
{dashboard_button or ""}
<p style="font-size:11px;color:#9CA3AF;margin-top:18px;border-top:1px solid #E5E7EB;padding-top:10px;">
  Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent
  (submission watcher). PDF: {os.path.basename(pdf_path) if pdf_path else "—"}.
</p>
</body>
</html>
"""


def _format_submission_telegram_body(
    s: "submission_watcher.Submission",
    dashboard_link: Optional[str] = None,
) -> str:
    notes = s.notes or "—"
    if len(notes) > 200:
        notes = notes[:197] + "…"
    trigger = s.trigger or "—"
    if len(trigger) > 200:
        trigger = trigger[:197] + "…"
    body = (
        f"📝 <b>UAE AID AGENCY</b> — New Project Submission\n"
        f"<i>Decision required: APPROVE / DEFER / REJECT</i>\n\n"
        f"<b>{s.project_name}</b>\n"
        f"<i>{s.submission_id} · {s.country} (Tier {s.tier}) · {s.location or '—'} · Submitted {s.submitted_date}</i>\n\n"
        f"• Trigger: {trigger}\n"
        f"• Modality: {s.proposed_modality or '—'}\n"
        f"• Amount: <b>{_fmt_money_usd(s.proposed_amount_usd)}</b>\n"
        f"• Implementer: {s.implementer or '—'}\n"
        f"• Duration: {s.duration or '—'}\n"
        f"• Approver: <b>{s.approver or '—'}</b>\n"
        f"• Submitted by: {s.submitted_by or '—'}\n"
        f"• Notes: {notes}\n\n"
        f"📎 Country executive summary attached."
    )
    if dashboard_link:
        body += dashboard_link
    return body


def _send_submission_alert(
    sub: "submission_watcher.Submission",
    recipients: List[str],
    tg_chat_ids: List,
    tg_enabled: bool,
    test_prefix: bool = False,
    dashboard_button: Optional[str] = None,
    dashboard_link: Optional[str] = None,
) -> bool:
    # Build BOTH PDFs: 1) country executive summary, 2) project brief.
    exec_pdf = None
    brief_pdf = None
    try:
        scores = compute_all_scores()
        match = next((sc for sc in scores if sc.country.lower() == sub.country.lower()), None)
        if match:
            exec_pdf = generate_pdf(match, previous_tier=None, output_dir=OUTPUT_DIR)
            _log(f"  Executive summary PDF: {exec_pdf}")
    except Exception as e:
        _log(f"  WARN: could not generate exec summary for submission ({sub.country}): {e}")
    try:
        brief_pdf = project_brief.generate_submission_brief(sub, OUTPUT_DIR)
        _log(f"  Project brief PDF: {brief_pdf}")
    except Exception as e:
        _log(f"  WARN: could not generate project brief ({sub.submission_id}): {e}")

    # Collect non-None attachment paths.
    attachments = [p for p in (exec_pdf, brief_pdf) if p]

    subject = _format_submission_email_subject(sub)
    # Pass the brief PDF name to the email body so the footer can reference it.
    email_body = _format_submission_email_body(sub, exec_pdf or "", dashboard_button=dashboard_button)
    telegram_body = _format_submission_telegram_body(sub, dashboard_link=dashboard_link)

    email_ok = tg_ok = True
    if recipients:
        email_ok = send_alert(recipients, subject, email_body, attachment_paths=attachments)
        if email_ok:
            _log(f"  Submission email sent to {recipients} ({sub.submission_id}) with {len(attachments)} PDF(s).")
        else:
            _log(f"  ERROR: submission email failed ({sub.submission_id}).")
    if tg_enabled and tg_chat_ids:
        tg_ok = telegram_sender.send_alert(
            tg_chat_ids, telegram_body, attachment_paths=attachments
        )
        if tg_ok:
            _log(f"  Submission Telegram sent to chat_ids {tg_chat_ids} ({sub.submission_id}).")
        else:
            _log(f"  ERROR: submission Telegram failed ({sub.submission_id}).")
    return email_ok and tg_ok


def _format_approval_email_subject(a: "approval_watcher.Approval") -> str:
    return f"[UAE Aid · Approval] {a.country} — {a.tier} — {_fmt_money_usd(a.amount_usd)}"


def _format_approval_email_body(
    a: "approval_watcher.Approval",
    pdf_path: str,
    dashboard_button: Optional[str] = None,
) -> str:
    return f"""\
<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1F2937;">
<table cellpadding="0" cellspacing="0" style="background:#1D252C;color:white;
       padding:14px 18px;border-radius:8px;border-top:3px solid #FBAE40;
       border-bottom:3px solid #FBAE40;width:100%;max-width:640px;">
  <tr><td>
    <div style="font-weight:700;letter-spacing:.5px;">UAE AID AGENCY</div>
    <div style="font-size:12px;color:#CBDCE6;">Project Approval / Sign-off</div>
  </td></tr>
</table>
<p style="font-size:16px;margin:18px 0 6px 0;">
  Project for <b>{a.country}</b> has been
  <b style="color:#2E7D32;">APPROVED</b>.
</p>
<p style="font-size:13px;color:#374151;margin:0 0 14px 0;">
  {a.crisis_id} · {a.tier} · Recorded {a.date}.
</p>
<table cellpadding="6" style="border-collapse:collapse;font-size:13px;">
  <tr><td style="color:#6B7280;">Country</td><td><b>{a.country}</b></td></tr>
  <tr><td style="color:#6B7280;">Crisis</td><td>{a.crisis_id}</td></tr>
  <tr><td style="color:#6B7280;">Tier</td><td><b>{a.tier}</b></td></tr>
  <tr><td style="color:#6B7280;">Decision</td><td><b style="color:#2E7D32;">{a.decision}</b></td></tr>
  <tr><td style="color:#6B7280;">Modality</td><td>{a.modality or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Amount</td><td><b>{_fmt_money_usd(a.amount_usd)}</b></td></tr>
  <tr><td style="color:#6B7280;">Lead Partner</td><td>{a.lead_partner or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Approver</td><td>{a.approver or "—"}</td></tr>
  <tr><td style="color:#6B7280;">Notes</td><td>{a.notes or "—"}</td></tr>
</table>
<p style="font-size:13px;margin-top:14px;">
  The country's executive summary is attached as a PDF for context.<br/>
  Recorded in <code>04_Response_Decision_Log.csv</code>.
</p>
{dashboard_button or ""}
<p style="font-size:11px;color:#9CA3AF;margin-top:18px;border-top:1px solid #E5E7EB;padding-top:10px;">
  Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent
  (approval watcher). PDF: {os.path.basename(pdf_path) if pdf_path else "—"}.
</p>
</body>
</html>
"""


def _format_approval_telegram_body(
    a: "approval_watcher.Approval",
    dashboard_link: Optional[str] = None,
) -> str:
    notes = a.notes or "—"
    if len(notes) > 200:
        notes = notes[:197] + "…"
    body = (
        f"✅ <b>UAE AID AGENCY</b> — Project Approved\n\n"
        f"<b>{a.country}</b> · {a.tier}\n"
        f"<i>{a.crisis_id} · Recorded {a.date}</i>\n\n"
        f"• Decision: <b>{a.decision}</b>\n"
        f"• Modality: {a.modality or '—'}\n"
        f"• Amount: <b>{_fmt_money_usd(a.amount_usd)}</b>\n"
        f"• Lead Partner: {a.lead_partner or '—'}\n"
        f"• Approver: {a.approver or '—'}\n"
        f"• Notes: {notes}\n\n"
        f"📎 Country executive summary attached."
    )
    if dashboard_link:
        body += dashboard_link
    return body


def _send_approval_alert(
    approval: "approval_watcher.Approval",
    recipients: List[str],
    tg_chat_ids: List,
    tg_enabled: bool,
    test_prefix: bool = False,
    dashboard_button: Optional[str] = None,
    dashboard_link: Optional[str] = None,
) -> bool:
    """Send one approval alert via every configured channel. Returns True iff all succeed."""
    # Build BOTH PDFs: country executive summary + approval brief (with logo).
    exec_pdf = None
    brief_pdf = None
    try:
        scores = compute_all_scores()
        match = next((s for s in scores if s.crisis_id == approval.crisis_id), None) \
                or next((s for s in scores if s.country.lower() == approval.country.lower()), None)
        if match:
            exec_pdf = generate_pdf(match, previous_tier=None, output_dir=OUTPUT_DIR)
            _log(f"  Executive summary PDF: {exec_pdf}")
    except Exception as e:
        _log(f"  WARN: could not generate exec summary for approval ({approval.country}): {e}")
    try:
        brief_pdf = project_brief.generate_approval_brief(approval, OUTPUT_DIR)
        _log(f"  Approval brief PDF: {brief_pdf}")
    except Exception as e:
        _log(f"  WARN: could not generate approval brief ({approval.country}): {e}")

    attachments = [p for p in (exec_pdf, brief_pdf) if p]

    subject = _format_approval_email_subject(approval)
    email_body = _format_approval_email_body(approval, exec_pdf or "", dashboard_button=dashboard_button)
    telegram_body = _format_approval_telegram_body(approval, dashboard_link=dashboard_link)

    email_ok = tg_ok = True
    if recipients:
        email_ok = send_alert(recipients, subject, email_body, attachment_paths=attachments)
        if email_ok:
            _log(f"  Approval email sent to {recipients} ({approval.country}) with {len(attachments)} PDF(s).")
        else:
            _log(f"  ERROR: approval email failed ({approval.country}).")
    if tg_enabled and tg_chat_ids:
        tg_ok = telegram_sender.send_alert(
            tg_chat_ids, telegram_body, attachment_paths=attachments
        )
        if tg_ok:
            _log(f"  Approval Telegram sent to chat_ids {tg_chat_ids} ({approval.country}).")
        else:
            _log(f"  ERROR: approval Telegram failed ({approval.country}).")
    return email_ok and tg_ok


def _format_telegram_body(
    score: CrisisScore,
    previous_tier: Optional[int],
    dashboard_link: Optional[str] = None,
) -> str:
    """Compact HTML message for Telegram. Stays under the 1024-char caption cap."""
    direction = _direction_phrase(previous_tier, score.decision_tier)
    tier_label = _tier_label(score.decision_tier)
    emoji = "🚨" if score.decision_tier == 1 else "⚠️"
    action = (
        "Mobilize ERT. Pledge within 7 days. Brief DG within 24h."
        if score.decision_tier == 1
        else "Pledge within 14 days. Multi-sector via UN / IFRC / INGO."
    )
    trigger = score.trigger_event or "—"
    if len(trigger) > 240:
        trigger = trigger[:237] + "…"
    body = (
        f"{emoji} <b>UAE AID AGENCY</b> — Tier {score.decision_tier} Escalation Alert\n\n"
        f"<b>{score.country}</b> has moved to <b>{tier_label}</b>.\n"
        f"<i>{score.crisis_id} · {score.region} · {score.crisis_type} · {direction}</i>\n\n"
        f"• Priority Score: <b>{score.priority_score:.1f}/100</b>\n"
        f"• ACAPS Severity: <b>{score.severity}/5</b>\n"
        f"• Trigger: {trigger}\n\n"
        f"<i>{action}</i>\n"
        f"📎 Full executive summary attached."
    )
    if dashboard_link:
        body += dashboard_link
    return body


def _format_email_body(
    score: CrisisScore,
    previous_tier: Optional[int],
    pdf_path: str,
    dashboard_button: Optional[str] = None,
) -> str:
    direction = _direction_phrase(previous_tier, score.decision_tier)
    tier_label = _tier_label(score.decision_tier)
    tier_color = "#7A0F0F" if score.decision_tier == 1 else "#B81D24"
    return f"""\
<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1F2937;">
<table cellpadding="0" cellspacing="0" style="background:#1D252C;color:white;
       padding:14px 18px;border-radius:8px;border-top:3px solid #FBAE40;
       border-bottom:3px solid #FBAE40;width:100%;max-width:640px;">
  <tr><td>
    <div style="font-weight:700;letter-spacing:.5px;">UAE AID AGENCY</div>
    <div style="font-size:12px;color:#CBDCE6;">Tier {score.decision_tier} Escalation Alert</div>
  </td></tr>
</table>
<p style="font-size:16px;margin:18px 0 6px 0;">
  <b>{score.country}</b> has moved to <b style="color:{tier_color};">Decision {tier_label}</b>.
</p>
<p style="font-size:13px;color:#374151;margin:0 0 14px 0;">
  {score.crisis_id} · {score.region} · {score.crisis_type} · {direction}.
</p>
<table cellpadding="6" style="border-collapse:collapse;font-size:13px;">
  <tr><td style="color:#6B7280;">Crisis name</td><td><b>{score.country} — {score.crisis_type}</b></td></tr>
  <tr><td style="color:#6B7280;">Location / Region</td><td>{score.region}</td></tr>
  <tr><td style="color:#6B7280;">New tier</td><td><b style="color:{tier_color};">{tier_label}</b> (Priority Score {score.priority_score:.1f}/100)</td></tr>
  <tr><td style="color:#6B7280;">Previous tier</td><td>{"Unknown (first run)" if previous_tier is None else f"Tier {previous_tier}"}</td></tr>
  <tr><td style="color:#6B7280;">Severity (ACAPS)</td><td>{score.severity}/5</td></tr>
  <tr><td style="color:#6B7280;">Trigger</td><td>{score.trigger_event or "—"}</td></tr>
</table>
<p style="font-size:13px;margin-top:14px;">
  The full executive summary is attached as a PDF.<br/>
  {_tier_action_line(score.decision_tier)}
</p>
{dashboard_button or ""}
<p style="font-size:11px;color:#9CA3AF;margin-top:18px;border-top:1px solid #E5E7EB;padding-top:10px;">
  Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent.
  Source files: 01_Active_Crises.csv, 02_Humanitarian_Indicators.csv,
  03_UAE_Priority_Countries.csv, 04_Response_Decision_Log.csv. PDF: {os.path.basename(pdf_path)}.
</p>
</body>
</html>
"""


def _parse_simulations(values: List[str]) -> Dict[str, int]:
    out = {}
    for v in values or []:
        if "=" in v:
            k, t = v.split("=", 1)
            try:
                out[k.strip()] = int(t.strip())
            except ValueError:
                pass
    return out


def _resolve_channels(config: dict):
    """Returns (email_recipients, telegram_chat_ids, telegram_enabled)."""
    recipients: List[str] = [r for r in (config.get("recipients") or []) if r and r.strip()]
    tg_cfg = config.get("telegram") or {}
    tg_enabled = bool(tg_cfg.get("enabled")) and _TELEGRAM_AVAILABLE
    tg_chat_ids = [c for c in (tg_cfg.get("chat_ids") or []) if c not in (None, "")]
    return recipients, tg_chat_ids, tg_enabled


def run(force: bool = False, dry_run: bool = False, simulate: Optional[Dict[str, int]] = None) -> int:
    """Execute one detection cycle. Returns the number of alerts sent."""
    config = _load_config()
    recipients, tg_chat_ids, tg_enabled = _resolve_channels(config)
    if not recipients and not (tg_enabled and tg_chat_ids):
        _log("ERROR: no recipients (email or Telegram) configured. Aborting.")
        return 0
    dashboard_button, dashboard_link, _ = _dashboard_link(config)

    scores = compute_all_scores()
    current: Dict[str, int] = {s.crisis_id: s.decision_tier for s in scores}

    # Apply simulation overrides if any (useful for end-to-end testing).
    if simulate:
        for cid, t in simulate.items():
            if cid in current:
                _log(f"SIMULATION: overriding {cid} tier {current[cid]} → {t}")
                current[cid] = t
        # Reflect the override in the score objects so the PDF/email show the simulated tier.
        for s in scores:
            if s.crisis_id in (simulate or {}):
                s.decision_tier = simulate[s.crisis_id]

    previous: Optional[Dict[str, int]] = _load_state()
    first_run = previous is None and not force

    if first_run:
        _log("First run detected — seeding state without sending alerts.")
        _save_state(current)
        return 0

    previous = previous or {}
    # Tier threshold: alert when a crisis is at this tier or worse (lower number = worse).
    # Default 2 means we alert on Tier 1 and Tier 2.
    threshold = int(config.get("alert_tier_threshold", 2))

    alerts: List[CrisisScore] = []
    for s in scores:
        cur_tier = current.get(s.crisis_id, s.decision_tier)
        prev_tier = previous.get(s.crisis_id)
        # Alert if current tier is at/above (numerically <=) threshold AND tier has changed
        # (or --force was passed). Movement between Tier 1 and Tier 2 also triggers an alert
        # because both are 'high response zone' and the direction matters for leadership.
        in_zone_now = cur_tier <= threshold
        in_zone_before = prev_tier is not None and prev_tier <= threshold
        is_transition = (not in_zone_before) or (prev_tier != cur_tier)
        if in_zone_now and (force or is_transition):
            alerts.append(s)

    n_t1 = sum(1 for t in current.values() if t == 1)
    n_t2 = sum(1 for t in current.values() if t == 2)
    _log(
        f"Checked {len(scores)} crises. "
        f"Currently at Tier 1: {n_t1}; Tier 2: {n_t2}. "
        f"Alerts to send (Tier ≤ {threshold} transitions): {len(alerts)}."
    )

    sent = 0
    # Track which crises had successful delivery this run. Anything that
    # failed (PDF render error OR email send failure) stays with its OLD
    # tier in the persisted state, so the next hourly run sees the same
    # transition and tries again.
    delivered_ids: set = set()

    for s in alerts:
        prev_tier = previous.get(s.crisis_id)
        try:
            pdf_path = generate_pdf(s, prev_tier, OUTPUT_DIR)
            _log(f"  PDF generated: {pdf_path}")
        except Exception as e:
            _log(f"  ERROR generating PDF for {s.country}: {e}\n{traceback.format_exc()}")
            continue

        if dry_run:
            channels_desc = []
            if recipients:
                channels_desc.append(f"email→{recipients}")
            if tg_enabled and tg_chat_ids:
                channels_desc.append(f"telegram→{tg_chat_ids}")
            _log(f"  DRY RUN — would send via: {', '.join(channels_desc)}")
            delivered_ids.add(s.crisis_id)
            continue

        subject = config.get(
            "subject_template", "[UAE Aid · Tier {tier} Alert] {country} — {crisis_id}"
        ).format(country=s.country, crisis_id=s.crisis_id, tier=s.decision_tier)
        email_body = _format_email_body(s, prev_tier, pdf_path, dashboard_button=dashboard_button)
        telegram_body = _format_telegram_body(s, prev_tier, dashboard_link=dashboard_link)

        # Send via each configured channel. A crisis is treated as fully
        # delivered only if EVERY configured channel succeeded.
        email_ok = True
        tg_ok = True
        if recipients:
            email_ok = send_alert(recipients, subject, email_body, attachment_path=pdf_path)
            if email_ok:
                _log(f"  Email sent to {recipients} — subject: {subject!r}")
            else:
                _log(f"  ERROR: email send failed for {s.country}.")
        if tg_enabled and tg_chat_ids:
            tg_ok = telegram_sender.send_alert(
                tg_chat_ids, telegram_body, attachment_path=pdf_path
            )
            if tg_ok:
                _log(f"  Telegram sent to chat_ids {tg_chat_ids}.")
            else:
                _log(f"  ERROR: Telegram send failed for {s.country}.")

        if email_ok and tg_ok:
            sent += 1
            delivered_ids.add(s.crisis_id)
        else:
            _log(
                f"  PDF still available at {pdf_path}. "
                f"Will retry on next hourly run (state not advanced for this crisis)."
            )

    # ── Submission watcher (new projects awaiting decision) ─────────
    try:
        new_subs = submission_watcher.detect_new_submissions()
    except Exception as e:
        _log(f"  WARN: submission watcher failed: {e}")
        new_subs = []

    if new_subs:
        _log(f"Found {len(new_subs)} new submission(s) awaiting decision.")
    delivered_subs: List[submission_watcher.Submission] = []
    for sub in new_subs:
        if dry_run:
            _log(f"  DRY RUN — would notify submission {sub.submission_id} ({sub.country}).")
            delivered_subs.append(sub)
            continue
        ok = _send_submission_alert(
            sub, recipients, tg_chat_ids, tg_enabled,
            dashboard_button=dashboard_button, dashboard_link=dashboard_link,
        )
        if ok:
            delivered_subs.append(sub)
            sent += 1
        else:
            _log(f"  Submission {sub.submission_id} will retry on next run (state not advanced).")

    if not dry_run and not simulate and delivered_subs:
        submission_watcher.mark_seen(delivered_subs)

    # ── Approval / sign-off watcher ──────────────────────────────────
    # Independent of tier transitions; checks the decision log for newly
    # added 'Approve' rows and fires the same email+Telegram channels.
    try:
        new_approvals = approval_watcher.detect_new_approvals()
    except Exception as e:
        _log(f"  WARN: approval watcher failed: {e}")
        new_approvals = []

    if new_approvals:
        _log(f"Found {len(new_approvals)} new approval(s) in the decision log.")
    delivered_approvals: List[approval_watcher.Approval] = []
    for ap in new_approvals:
        if dry_run:
            _log(f"  DRY RUN — would notify approval for {ap.country} ({ap.tier}, {ap.crisis_id}).")
            delivered_approvals.append(ap)
            continue
        ok = _send_approval_alert(
            ap, recipients, tg_chat_ids, tg_enabled,
            dashboard_button=dashboard_button, dashboard_link=dashboard_link,
        )
        if ok:
            delivered_approvals.append(ap)
            sent += 1
        else:
            _log(f"  Approval for {ap.country} will retry on next run (state not advanced).")

    # Persist approval keys ONLY for successfully delivered approvals.
    if not dry_run and not simulate and delivered_approvals:
        approval_watcher.mark_seen(delivered_approvals)

    # Persist new state, but ONLY for crises whose alert was successfully
    # delivered (or that didn't need an alert at all). Anything that failed
    # to send keeps its old tier so the next run re-detects the transition.
    if not dry_run and not simulate:
        next_state = dict(previous)  # start from previous
        for cid, cur_tier in current.items():
            prev_tier = previous.get(cid)
            needed_alert = (cur_tier <= threshold) and (
                prev_tier is None or prev_tier != cur_tier
            )
            if not needed_alert:
                # No alert was required; safe to advance state.
                next_state[cid] = cur_tier
            elif cid in delivered_ids:
                # Alert needed AND delivered successfully.
                next_state[cid] = cur_tier
            # else: alert was needed but failed — leave state unchanged so
            # we retry on the next run.
        _save_state(next_state)

    return sent


def test_email(crisis_id: Optional[str] = None) -> int:
    """Send a single test alert without touching state.

    Picks one crisis (preferring the requested ID, else the highest-scoring
    Tier 2 crisis, else any Tier 1 crisis) and pushes it through the full
    pipeline: PDF generation + email send + Telegram send. Useful for
    verifying every configured channel is wired correctly end-to-end.
    """
    config = _load_config()
    recipients, tg_chat_ids, tg_enabled = _resolve_channels(config)
    if not recipients and not (tg_enabled and tg_chat_ids):
        _log("ERROR: no recipients (email or Telegram) configured. Aborting test.")
        return 1
    dashboard_button, dashboard_link, _ = _dashboard_link(config)

    scores = compute_all_scores()
    target: Optional[CrisisScore] = None
    if crisis_id:
        target = next((s for s in scores if s.crisis_id == crisis_id), None)
        if not target:
            _log(f"ERROR: crisis_id {crisis_id!r} not found.")
            return 1
    else:
        t2 = sorted([s for s in scores if s.decision_tier == 2],
                    key=lambda s: -s.priority_score)
        t1 = sorted([s for s in scores if s.decision_tier == 1],
                    key=lambda s: -s.priority_score)
        target = (t2 + t1)[0] if (t2 + t1) else None
    if not target:
        _log("ERROR: no Tier 1 or Tier 2 crisis found to use for the test.")
        return 1

    _log(f"TEST EMAIL: using {target.crisis_id} ({target.country}) at Tier {target.decision_tier}.")
    try:
        pdf_path = generate_pdf(target, previous_tier=target.decision_tier + 1, output_dir=OUTPUT_DIR)
        _log(f"  PDF generated: {pdf_path}")
    except Exception as e:
        _log(f"  ERROR generating PDF: {e}\n{traceback.format_exc()}")
        return 1

    subject = config.get(
        "subject_template", "[UAE Aid · Tier {tier} Alert] {country} — {crisis_id}"
    ).format(country=target.country, crisis_id=target.crisis_id, tier=target.decision_tier)
    email_body = _format_email_body(
        target, previous_tier=target.decision_tier + 1, pdf_path=pdf_path,
        dashboard_button=dashboard_button,
    )
    telegram_body = _format_telegram_body(
        target, previous_tier=target.decision_tier + 1,
        dashboard_link=dashboard_link,
    )

    email_ok = tg_ok = True
    if recipients:
        email_ok = send_alert(recipients, subject, email_body, attachment_path=pdf_path)
        if email_ok:
            _log(f"  Test email sent to {recipients}.")
        else:
            _log(f"  ERROR: test email send failed.")
    if tg_enabled and tg_chat_ids:
        tg_ok = telegram_sender.send_alert(
            tg_chat_ids, telegram_body, attachment_path=pdf_path
        )
        if tg_ok:
            _log(f"  Test Telegram message sent to chat_ids {tg_chat_ids}.")
        else:
            _log(f"  ERROR: test Telegram send failed.")

    if email_ok and tg_ok:
        channels = []
        if recipients: channels.append(f"email {recipients}")
        if tg_enabled and tg_chat_ids: channels.append(f"telegram {tg_chat_ids}")
        print(f"Done. Test alert sent via {', '.join(channels)} for {target.country} (Tier {target.decision_tier}).")
        return 0
    print(f"PARTIAL or FULL failure. See log above; PDF is still at {pdf_path}.")
    return 1


def test_approval(crisis_id: Optional[str] = None) -> int:
    """Send a single test APPROVAL alert without touching state.

    Picks the most recent Approve row in 04_Response_Decision_Log.csv
    (or the one matching crisis_id if provided), formats it as a sign-off
    notification, and pushes it through email + Telegram with the country
    executive summary PDF attached.
    """
    config = _load_config()
    recipients, tg_chat_ids, tg_enabled = _resolve_channels(config)
    if not recipients and not (tg_enabled and tg_chat_ids):
        _log("ERROR: no recipients (email or Telegram) configured. Aborting test.")
        return 1

    rows = approval_watcher.detect_new_approvals(force_all=True)
    target = None
    if crisis_id:
        target = next((r for r in rows if r.crisis_id == crisis_id), None)
        if not target:
            _log(f"ERROR: no Approve row found for crisis_id {crisis_id!r}.")
            return 1
    else:
        # Most recent by date.
        rows_sorted = sorted(rows, key=lambda r: r.date, reverse=True)
        target = rows_sorted[0] if rows_sorted else None
    if not target:
        _log("ERROR: 04_Response_Decision_Log.csv contains no Approve rows.")
        return 1

    dashboard_button, dashboard_link, _ = _dashboard_link(config)
    _log(f"TEST APPROVAL: using {target.crisis_id} ({target.country}) recorded {target.date}.")
    ok = _send_approval_alert(
        target, recipients, tg_chat_ids, tg_enabled, test_prefix=True,
        dashboard_button=dashboard_button, dashboard_link=dashboard_link,
    )
    if ok:
        channels = []
        if recipients: channels.append(f"email {recipients}")
        if tg_enabled and tg_chat_ids: channels.append(f"telegram {tg_chat_ids}")
        print(f"Done. Test APPROVAL alert sent via {', '.join(channels)} for {target.country}.")
        return 0
    print("PARTIAL or FULL failure on test approval. See log above.")
    return 1


def test_submission(submission_id: Optional[str] = None) -> int:
    """Send a single test SUBMISSION alert.

    Picks the most recently-submitted row from 06_Pending_Submissions.csv
    (or the one matching submission_id if provided), formats it as a
    submission-awaiting-decision notification, and pushes it through
    email + Telegram with the country exec summary PDF attached.

    If the CSV is empty, returns 1 with a clear message.
    """
    config = _load_config()
    recipients, tg_chat_ids, tg_enabled = _resolve_channels(config)
    if not recipients and not (tg_enabled and tg_chat_ids):
        _log("ERROR: no recipients (email or Telegram) configured. Aborting test.")
        return 1

    rows = submission_watcher.detect_new_submissions(force_all=True)
    if not rows:
        print(
            "06_Pending_Submissions.csv is empty (no rows beyond the header). "
            "Add a sample row via the dashboard's Submit-for-Approval modal — "
            "the dashboard will offer to download a fresh CSV; save it over "
            "06_Pending_Submissions.csv in the dashboard folder, then re-run."
        )
        return 1

    target = None
    if submission_id:
        target = next((r for r in rows if r.submission_id == submission_id), None)
        if not target:
            _log(f"ERROR: no submission row found for ID {submission_id!r}.")
            return 1
    else:
        target = sorted(rows, key=lambda r: r.submitted_date, reverse=True)[0]

    dashboard_button, dashboard_link, _ = _dashboard_link(config)
    _log(f"TEST SUBMISSION: using {target.submission_id} ({target.country}, {target.project_name}).")
    ok = _send_submission_alert(
        target, recipients, tg_chat_ids, tg_enabled, test_prefix=True,
        dashboard_button=dashboard_button, dashboard_link=dashboard_link,
    )
    if ok:
        channels = []
        if recipients: channels.append(f"email {recipients}")
        if tg_enabled and tg_chat_ids: channels.append(f"telegram {tg_chat_ids}")
        print(f"Done. Test SUBMISSION alert sent via {', '.join(channels)} for {target.submission_id}.")
        return 0
    print("PARTIAL or FULL failure on test submission. See log above.")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="UAE Aid Agency Tier-2+ Alert Agent")
    p.add_argument("--force", action="store_true", help="Alert on every current Tier 1/2, ignoring prior state.")
    p.add_argument("--dry-run", action="store_true", help="Compute and log only; do not send emails.")
    p.add_argument(
        "--simulate",
        action="append",
        default=[],
        metavar="ID=TIER",
        help="Pretend a crisis has this tier this run (e.g. C-013=2). Repeatable.",
    )
    p.add_argument(
        "--test-email",
        nargs="?",
        const="__pick__",
        default=None,
        metavar="CRISIS_ID",
        help="Send a single test alert (optionally for a specific crisis ID, "
             "e.g. --test-email C-013) without modifying state.",
    )
    p.add_argument(
        "--test-approval",
        nargs="?",
        const="__pick__",
        default=None,
        metavar="CRISIS_ID",
        help="Send a single test APPROVAL alert (latest Approve row in the "
             "decision log, or one for a specific CRISIS_ID).",
    )
    p.add_argument(
        "--test-submission",
        nargs="?",
        const="__pick__",
        default=None,
        metavar="SUBMISSION_ID",
        help="Send a single test SUBMISSION alert (latest pending submission, "
             "or one for a specific SUBMISSION_ID like PA-2026-001).",
    )
    args = p.parse_args(argv)
    if args.test_email is not None:
        chosen = None if args.test_email == "__pick__" else args.test_email
        return test_email(chosen)
    if args.test_approval is not None:
        chosen = None if args.test_approval == "__pick__" else args.test_approval
        return test_approval(chosen)
    if args.test_submission is not None:
        chosen = None if args.test_submission == "__pick__" else args.test_submission
        return test_submission(chosen)
    sent = run(force=args.force, dry_run=args.dry_run, simulate=_parse_simulations(args.simulate))
    print(f"Done. {sent} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
