"""
UAE Aid Agency — Tier-2 Alert Agent
Executive Summary PDF generator.

Builds a one-to-two page PDF executive summary for a single crisis that
has newly moved to Decision Tier 2 (Strong Response).

Uses reportlab. Auto-installs reportlab from PyPI on first run if missing.

The PDF is styled to match the dashboard's visual identity (navy + gold).
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from typing import Optional

from scoring import CrisisScore


def _ensure_reportlab() -> None:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        print("[exec_summary] reportlab not found — installing...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "reportlab"]
        )


_ensure_reportlab()

# Imports below depend on reportlab being installed.
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT, TA_RIGHT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#1D252C")
NAVY2 = colors.HexColor("#2F586E")
GOLD = colors.HexColor("#FBAE40")
TIER2_RED = colors.HexColor("#B81D24")
GREY = colors.HexColor("#6B7280")
LIGHT_BG = colors.HexColor("#ECF1F6")
LINE = colors.HexColor("#DDE3EA")

# Logo file locations (resolved relative to the dashboard folder, which is
# the parent of this script's folder). Falls back gracefully if the files
# aren't found at runtime.
_DASHBOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_FULL_PATH = os.path.join(_DASHBOARD_DIR, "uae_logo_full.png")
LOGO_PATH = os.path.join(_DASHBOARD_DIR, "uae_logo.png")
EMBLEM_PATH = os.path.join(_DASHBOARD_DIR, "uae_aid_agency_emblem.png")


def _fmt_thousands(value_thousands: float) -> str:
    """Format a number that is already expressed in thousands."""
    if value_thousands <= 0:
        return "—"
    if value_thousands >= 1000:
        return f"{value_thousands/1000:.1f} M"
    return f"{value_thousands:,.0f} K"


def _fmt_pct(v: float) -> str:
    return f"{v:.0f}%"


def _recommended_actions(s: CrisisScore) -> list:
    """Tier-specific actions per methodology section 4.3."""
    if s.decision_tier == 1:
        return [
            "Mobilize the UAE Emergency Response Team immediately (per UAE methodology §4.3, Tier 1).",
            "Pledge within 7 days of this tier transition.",
            f"Consider deploying UAE Search & Rescue / medical / aircraft mission to {s.country}.",
            "Initiate high-level political signalling; brief MoFAIC for concurrence.",
            f"Convene Country Desk + Operations to scope multi-channel response for {s.country}.",
            "Engage UN agencies, IFRC, and trusted INGOs as multi-sector lead partners.",
            f"Initiate weekly indicator refresh cadence for {s.country} (§7).",
            "Prepare Director-General decision brief within 24 hours of this transition.",
        ]
    # Tier 2 default
    return [
        "Pledge within 14 days of tier transition (per UAE methodology §4.3, Tier 2).",
        f"Convene Country Desk + Operations to confirm response modality for {s.country}.",
        "Engage UN agencies, IFRC, or trusted INGOs as lead partners (multi-sector).",
        "Notify MoFAIC liaison; align with regional partners (OIC, Arab League, AU as applicable).",
        f"Initiate weekly indicator refresh cadence for {s.country} (Tier 1/2 cadence per §7).",
        "Prepare decision brief for Director-General within 48 hours of this transition.",
    ]


def _tier_meta(t: int) -> tuple:
    """Returns (display_label, header_color) for the given tier."""
    if t == 1:
        return ("TIER 1 — IMMEDIATE RESPONSE", colors.HexColor("#7A0F0F"))
    return ("TIER 2 — STRONG RESPONSE", TIER2_RED)


def generate_pdf(score: CrisisScore, previous_tier: Optional[int], output_dir: str) -> str:
    """Generate a PDF executive summary. Returns the absolute path written."""
    os.makedirs(output_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    safe_country = "".join(c if c.isalnum() else "_" for c in score.country)
    tier_label, tier_color = _tier_meta(score.decision_tier)
    out_path = os.path.join(
        output_dir,
        f"UAE_Tier{score.decision_tier}_ExecSummary_{safe_country}_{today}.pdf",
    )

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Tier {score.decision_tier} Alert — {score.country}",
        author="UAE Aid Agency — Monitoring & Analysis Unit",
    )

    styles = getSampleStyleSheet()
    h_style = ParagraphStyle(
        "h", parent=styles["Heading1"], textColor=NAVY, fontSize=18,
        leading=22, spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"], textColor=GREY, fontSize=10,
        leading=13, spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "section", parent=styles["Heading2"], textColor=NAVY, fontSize=12,
        leading=16, spaceBefore=10, spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "body", parent=styles["Normal"], fontSize=10, leading=14, textColor=colors.HexColor("#1F2937"),
    )
    tier_chip = ParagraphStyle(
        "chip", parent=styles["Normal"], fontSize=14, leading=18,
        textColor=colors.white, alignment=TA_LEFT,
    )

    story: list = []

    # ---- Logo strip (UAE Aid Agency brand) ----
    logo_to_use = None
    for candidate in (LOGO_FULL_PATH, LOGO_PATH, EMBLEM_PATH):
        if os.path.exists(candidate):
            logo_to_use = candidate
            break
    if logo_to_use:
        try:
            logo_img = Image(logo_to_use, width=42 * mm, height=18 * mm,
                              kind="proportional")
            logo_row = Table(
                [[logo_img,
                  Paragraph(
                      "<para align='right'>"
                      "<font size=8 color='#6B7280'>UAE Aid Agency</font><br/>"
                      "<font size=8 color='#6B7280'>Monitoring &amp; Analysis Unit</font>"
                      "</para>",
                      styles["Normal"],
                  )]],
                colWidths=[60 * mm, 115 * mm],
            )
            logo_row.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(logo_row)
            story.append(Spacer(1, 8))
        except Exception:
            # If the image fails to load (e.g., unsupported format), skip
            # silently — the navy header band below still carries the brand.
            pass

    # ---- Header band ----
    header_data = [[
        Paragraph("<b>UAE AID AGENCY</b><br/>"
                  f"<font size=8 color='#CBDCE6'>Monitoring &amp; Analysis Unit · "
                  f"Tier {score.decision_tier} Escalation Alert</font>", tier_chip),
        Paragraph(f"<para align='right'><font size=9 color='#CBDCE6'>"
                  f"Issued {dt.datetime.now().strftime('%d %b %Y · %H:%M')}<br/>"
                  f"Recipient: Senior Leadership</font></para>", tier_chip),
    ]]
    header_tbl = Table(header_data, colWidths=[110 * mm, 65 * mm])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, NAVY),
        ("LINEABOVE", (0, 0), (-1, 0), 2, GOLD),
        ("LINEBELOW", (0, 0), (-1, -1), 2, GOLD),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 10))

    # ---- Title ----
    story.append(Paragraph(
        f"{score.country} — Tier {score.decision_tier} Alert",
        h_style,
    ))
    if previous_tier is None:
        direction = f"Newly classified at Tier {score.decision_tier}"
    elif previous_tier == score.decision_tier:
        direction = f"Reconfirmed at Tier {score.decision_tier}"
    elif previous_tier > score.decision_tier:
        direction = f"Escalation from Tier {previous_tier} → Tier {score.decision_tier}"
    else:
        direction = f"De-escalation from Tier {previous_tier} → Tier {score.decision_tier}"
    story.append(Paragraph(
        f"{score.crisis_id} · {score.region} · {score.crisis_type} · Status: {score.status} · "
        f"<b>{direction}</b>",
        sub_style,
    ))

    # ---- Key facts card ----
    tier_action_blurb = (
        "TIER 1 — Immediate Response (mobilize ERT; pledge within 7 days)"
        if score.decision_tier == 1
        else "TIER 2 — Strong Response (pledge within 14 days)"
    )
    key_card = [
        ["Crisis name", f"{score.country} — {score.crisis_type}"],
        ["Location / Region", score.region],
        ["New decision tier", tier_action_blurb],
        ["Priority score", f"{score.priority_score:.1f} / 100"],
        ["Previous tier", "Unknown (first run)" if previous_tier is None else f"Tier {previous_tier}"],
        ["ACAPS severity", f"{score.severity} / 5"],
        ["People in Need", _fmt_thousands(score.pin_thousands)],
        ["Displaced (IDPs + refugees)", _fmt_thousands(score.displaced_thousands)],
        ["Casualties (confirmed)", _fmt_thousands(score.casualties_thousands)],
        ["IPC Phase 3+", _fmt_thousands(score.ipc_phase3_thousands)],
        ["Children with SAM", _fmt_thousands(score.children_malnourished_thousands)],
        ["Health facilities damaged", _fmt_pct(score.health_facilities_damaged_pct)],
        ["WASH access below std", _fmt_pct(score.wash_below_std_pct)],
        ["Appeal funded", _fmt_pct(score.appeal_funded_pct)],
        ["Access constraints", f"{score.access_constraints} / 5"],
        ["UAE priority list", "Not listed" if not score.priority_tier else f"Tier {score.priority_tier}"],
        ["Past UAE engagements", str(score.track_record_count)],
        ["Source date", score.last_updated or "—"],
    ]
    tbl = Table(
        [[Paragraph(f"<b>{k}</b>", body_style), Paragraph(str(v), body_style)] for k, v in key_card],
        colWidths=[55 * mm, 120 * mm],
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(Paragraph("Key facts", section_style))
    story.append(tbl)

    # ---- Trigger event ----
    story.append(Paragraph("Trigger / situation summary", section_style))
    story.append(Paragraph(score.trigger_event or "No trigger note recorded.", body_style))

    # ---- Score breakdown ----
    story.append(Paragraph("Priority Score breakdown (methodology §4)", section_style))
    breakdown = [
        ["Component", "Raw score", "Weight", "Weighted contribution"],
        ["Mandate Fit", f"{score.mandate_fit:.1f}", "30%", f"{0.30 * score.mandate_fit:.2f}"],
        ["Severity (ACAPS)", f"{score.severity_score:.1f}", "25%", f"{0.25 * score.severity_score:.2f}"],
        ["Affected Population", f"{score.affected_pop_score:.1f}", "20%", f"{0.20 * score.affected_pop_score:.2f}"],
        ["Response Gap", f"{score.response_gap_score:.1f}", "15%", f"{0.15 * score.response_gap_score:.2f}"],
        ["Media / Political (Access)", f"{score.media_political_score:.1f}", "10%", f"{0.10 * score.media_political_score:.2f}"],
        ["Composite Priority Score", "", "", f"{score.priority_score:.2f}"],
    ]
    btbl = Table(breakdown, colWidths=[60 * mm, 30 * mm, 25 * mm, 45 * mm])
    btbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BG),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(btbl)

    # ---- Recommended actions ----
    story.append(Paragraph(
        f"Recommended actions (per methodology §4.3 — Tier {score.decision_tier})",
        section_style,
    ))
    bullets = "<br/>".join(f"&bull; {a}" for a in _recommended_actions(score))
    story.append(Paragraph(bullets, body_style))

    # ---- Footer ----
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "<font color='#6B7280' size=8>"
        "Generated automatically by the UAE Aid Agency Tier-2 Alert Agent. "
        "This document is a decision aid; per methodology §11 the Director-General "
        "retains discretion to escalate, de-escalate, or override the recommended tier."
        "</font>",
        body_style,
    ))

    doc.build(story)
    return out_path


if __name__ == "__main__":
    # Smoke test: generate a PDF for every current Tier 1 and Tier 2 crisis.
    from scoring import compute_all_scores
    scores = compute_all_scores()
    targets = [s for s in scores if s.decision_tier in (1, 2)]
    if not targets:
        print("No Tier 1 or Tier 2 crises in current data.")
    else:
        out_dir = os.path.join(os.path.dirname(__file__), "output")
        for s in targets:
            print(f"Generating Tier {s.decision_tier}:", s.country)
            path = generate_pdf(s, previous_tier=s.decision_tier + 1, output_dir=out_dir)
            print("  →", path)
