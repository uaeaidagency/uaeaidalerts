"""
UAE Aid Agency — Tier-2+ Alert Agent
Project Brief PDF generator.

Distinct from `exec_summary.py` (which is the country-level executive
summary). This module produces a project-focused one-page brief: what
is being requested or what was approved, by whom, with what modality,
amount, partner, and a decision/sign-off block.

Attached to:
  - Submission alert emails (alongside the country executive summary)
  - Approval alert emails (alongside the country executive summary)
  - Downloadable from the dashboard's Approvals tab (one-click)
"""
from __future__ import annotations

import datetime as dt
import os

# Re-use the brand colors, logo paths, and reportlab bootstrap from exec_summary.
from exec_summary import (  # noqa: F401
    NAVY, GOLD, GREY, LIGHT_BG, LINE, LOGO_FULL_PATH, LOGO_PATH, EMBLEM_PATH,
)
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


def _fmt_money_usd(usd: float) -> str:
    if not usd or usd <= 0:
        return "—"
    if usd >= 1_000_000:
        return f"USD {usd/1_000_000:,.2f} M"
    if usd >= 1_000:
        return f"USD {usd/1_000:,.0f} K"
    return f"USD {usd:,.0f}"


def _logo_to_use() -> str:
    for candidate in (LOGO_FULL_PATH, LOGO_PATH, EMBLEM_PATH):
        if os.path.exists(candidate):
            return candidate
    return ""


def _brand_header(story: list, styles, title: str, subtitle: str) -> None:
    """Logo strip + navy header band identical to exec_summary visuals."""
    chip = ParagraphStyle(
        "chip", parent=styles["Normal"], fontSize=14, leading=18,
        textColor=colors.white, alignment=TA_LEFT,
    )
    logo = _logo_to_use()
    if logo:
        try:
            logo_img = Image(logo, width=42 * mm, height=18 * mm, kind="proportional")
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
            pass

    band = Table([[
        Paragraph(
            f"<b>UAE AID AGENCY</b><br/>"
            f"<font size=8 color='#CBDCE6'>{title} · {subtitle}</font>",
            chip,
        ),
        Paragraph(
            f"<para align='right'><font size=9 color='#CBDCE6'>"
            f"Issued {dt.datetime.now().strftime('%d %b %Y · %H:%M')}<br/>"
            f"Recipient: Senior Leadership</font></para>",
            chip,
        ),
    ]], colWidths=[110 * mm, 65 * mm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LINEABOVE", (0, 0), (-1, 0), 2, GOLD),
        ("LINEBELOW", (0, 0), (-1, -1), 2, GOLD),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(band)
    story.append(Spacer(1, 12))


def generate_submission_brief(submission, output_dir: str) -> str:
    """One-page brief for a pending submission awaiting APPROVE/DEFER/REJECT."""
    os.makedirs(output_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    safe = "".join(ch if ch.isalnum() else "_"
                   for ch in (submission.project_name or submission.country or submission.submission_id))[:60]
    out_path = os.path.join(output_dir, f"UAE_Brief_Submission_{safe}_{today}.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Project Brief — {submission.project_name or submission.country}",
        author="UAE Aid Agency — Monitoring & Analysis Unit",
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14,
                          textColor=colors.HexColor("#1F2937"))
    h = ParagraphStyle("h", parent=styles["Heading1"], textColor=NAVY, fontSize=18,
                       leading=22, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=GREY, fontSize=10,
                          leading=13, spaceAfter=10)
    section = ParagraphStyle("section", parent=styles["Heading2"], textColor=NAVY, fontSize=12,
                              leading=16, spaceBefore=10, spaceAfter=4)

    story: list = []
    _brand_header(story, styles,
                   title="PROJECT SIGN-OFF BRIEF",
                   subtitle="Decision Required: APPROVE / DEFER / REJECT")

    story.append(Paragraph(submission.project_name or submission.country, h))
    story.append(Paragraph(
        f"{submission.submission_id} · {submission.country} · {submission.tier} · "
        f"<b><font color='#E07A00'>AWAITING DECISION</font></b>",
        sub,
    ))

    story.append(Paragraph("Project Details", section))
    rows = [
        ("Submission ID", submission.submission_id),
        ("Country", submission.country),
        ("Decision Tier", submission.tier),
        ("Project Name", submission.project_name or "—"),
        ("Trigger / Need", submission.trigger or "—"),
        ("Proposed Modality", submission.proposed_modality or "—"),
        ("Proposed Amount", _fmt_money_usd(submission.proposed_amount_usd)),
        ("Implementing Partner", submission.implementer or "—"),
        ("Duration", submission.duration or "—"),
        ("Point of Contact", submission.point_of_contact or "—"),
        ("Approver Requested", submission.approver or "—"),
        ("Submitted By", submission.submitted_by or "—"),
        ("Submitted Date", submission.submitted_date or "—"),
        ("Notes", submission.notes or "—"),
    ]
    tbl = Table(
        [[Paragraph(f"<b>{k}</b>", body), Paragraph(str(v), body)] for k, v in rows],
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
    story.append(tbl)

    tier_days = "7 days" if "TIER 1" in (submission.tier or "").upper() else "14 days"
    story.append(Paragraph("Decision Required", section))
    story.append(Paragraph(
        f"This submission is awaiting decision by <b>{submission.approver or 'designated approver'}</b>. "
        f"Per methodology §4.3, the recommended response window for {submission.tier or 'this tier'} is "
        f"<b>{tier_days}</b> from submission. The approver may select one of:",
        body,
    ))
    decision_tbl = Table([
        [Paragraph("<b><font color='#2E7D32'>APPROVE</font></b>", body),
         Paragraph("Authorize the project; record decision in 04_Response_Decision_Log.csv.", body)],
        [Paragraph("<b><font color='#E07A00'>DEFER</font></b>", body),
         Paragraph("Postpone for additional information; specify trigger to reconsider.", body)],
        [Paragraph("<b><font color='#B81D24'>REJECT</font></b>", body),
         Paragraph("Decline; provide a written rationale referencing the methodology criterion.", body)],
    ], colWidths=[28 * mm, 147 * mm])
    decision_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(decision_tbl)

    # Sign-off block
    story.append(Spacer(1, 14))
    sign = Table([[
        Paragraph("<b>Approver Signature</b><br/>"
                   "<font size=8 color='#9CA3AF'>____________________________</font>", body),
        Paragraph("<b>Decision</b><br/>"
                   "<font size=8 color='#9CA3AF'>&#9744; Approve &nbsp; &#9744; Defer &nbsp; &#9744; Reject</font>", body),
        Paragraph("<b>Date</b><br/>"
                   "<font size=8 color='#9CA3AF'>____________</font>", body),
    ]], colWidths=[70 * mm, 65 * mm, 40 * mm])
    sign.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.25, LINE),
    ]))
    story.append(sign)

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "<font color='#6B7280' size=8>"
        "Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent. "
        "Per methodology §11, the Director-General retains discretion to escalate, "
        "de-escalate, or override the recommended action."
        "</font>", body,
    ))

    doc.build(story)
    return out_path


def generate_approval_brief(approval, output_dir: str) -> str:
    """One-page brief for an approved/recorded decision."""
    os.makedirs(output_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    safe = "".join(ch if ch.isalnum() else "_"
                   for ch in (approval.country or approval.crisis_id))[:60]
    out_path = os.path.join(output_dir, f"UAE_Brief_Approval_{safe}_{today}.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Approval Brief — {approval.country}",
        author="UAE Aid Agency — Monitoring & Analysis Unit",
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14,
                          textColor=colors.HexColor("#1F2937"))
    h = ParagraphStyle("h", parent=styles["Heading1"], textColor=NAVY, fontSize=18,
                       leading=22, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=GREY, fontSize=10,
                          leading=13, spaceAfter=10)
    section = ParagraphStyle("section", parent=styles["Heading2"], textColor=NAVY, fontSize=12,
                              leading=16, spaceBefore=10, spaceAfter=4)

    story: list = []
    _brand_header(story, styles,
                   title="PROJECT APPROVAL CONFIRMATION",
                   subtitle="Decision Recorded · UAE Response Decision Log")

    story.append(Paragraph(approval.country, h))
    story.append(Paragraph(
        f"{approval.crisis_id} · {approval.tier} · Recorded {approval.date} · "
        f"<b><font color='#2E7D32'>{approval.decision.upper()}</font></b>",
        sub,
    ))

    story.append(Paragraph("Decision Details", section))
    rows = [
        ("Crisis ID", approval.crisis_id),
        ("Country", approval.country),
        ("Decision Tier", approval.tier),
        ("Decision", approval.decision),
        ("Modality", approval.modality or "—"),
        ("Amount", _fmt_money_usd(approval.amount_usd)),
        ("Lead Partner", approval.lead_partner or "—"),
        ("Approver", approval.approver or "—"),
        ("Recorded Date", approval.date or "—"),
        ("Notes", approval.notes or "—"),
    ]
    tbl = Table(
        [[Paragraph(f"<b>{k}</b>", body), Paragraph(str(v), body)] for k, v in rows],
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
    story.append(tbl)

    story.append(Paragraph("Status", section))
    story.append(Paragraph(
        f"This project has been <b><font color='#2E7D32'>{approval.decision.upper()}D</font></b> "
        f"by <b>{approval.approver or 'designated approver'}</b> and is now active. "
        f"Operations will execute the response through {approval.lead_partner or 'the designated partner'}. "
        f"The Monitoring Unit will refresh indicators weekly for this Tier 1/2 case (per methodology §7).",
        body,
    ))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "<font color='#6B7280' size=8>"
        "Generated automatically by the UAE Aid Agency Tier-2+ Alert Agent. "
        "Source: 04_Response_Decision_Log.csv."
        "</font>", body,
    ))

    doc.build(story)
    return out_path
