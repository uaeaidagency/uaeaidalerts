"""
UAE Aid Agency — Tier-2+ Alert Agent
Email sender.

PRIMARY: Gmail (or any SMTP server). Reads credentials from `smtp.env`
next to this file (key=value lines):

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=alerts.uaeaid@gmail.com
    SMTP_PASS=<16-char Gmail app password>
    SMTP_FROM=alerts.uaeaid@gmail.com
    SMTP_FROM_NAME=UAE Aid Agency Monitoring Unit

Gmail requires an App Password (not your regular Google password) — create
one at https://myaccount.google.com/apppasswords after enabling 2-Step
Verification. The 16-char string goes into SMTP_PASS verbatim (spaces are
optional and stripped automatically).

FALLBACK: Outlook desktop on Windows via COM. Only used if smtp.env is
missing or incomplete. Drives the locally logged-in Outlook profile —
no credentials needed, but Outlook must be installed and signed in.
"""
from __future__ import annotations

import os
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SMTP_ENV_FILE = os.path.join(HERE, "smtp.env")


def _read_smtp_env() -> dict:
    if not os.path.exists(SMTP_ENV_FILE):
        return {}
    out = {}
    with open(SMTP_ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            # Gmail app passwords are 16 chars often displayed with spaces — strip them.
            if k.strip().upper() == "SMTP_PASS":
                v = v.replace(" ", "")
            out[k.strip()] = v
    return out


def _normalize_attachments(attachment_paths) -> list:
    """Accept None, a single path, or an iterable of paths. Return a clean list."""
    if not attachment_paths:
        return []
    if isinstance(attachment_paths, (str, bytes, os.PathLike)):
        attachment_paths = [attachment_paths]
    return [str(p) for p in attachment_paths if p and os.path.exists(str(p))]


def _try_outlook_com(
    to: Iterable[str],
    subject: str,
    body_html: str,
    attachment_paths,
) -> bool:
    """Send via Outlook COM. Returns True on success, False if Outlook is unavailable."""
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("[emailer] pywin32 not installed; attempting auto-install...", file=sys.stderr)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--user", "pywin32"]
            )
            import win32com.client  # type: ignore  # noqa: F401
        except Exception as e:
            print(f"[emailer] pywin32 install failed: {e}", file=sys.stderr)
            return False

    try:
        import win32com.client  # type: ignore
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = ";".join(to)
        mail.Subject = subject
        mail.HTMLBody = body_html
        for p in _normalize_attachments(attachment_paths):
            mail.Attachments.Add(os.path.abspath(p))
        mail.Send()
        return True
    except Exception as e:
        print(f"[emailer] Outlook COM send failed: {e}", file=sys.stderr)
        return False


def _try_smtp(
    to: Iterable[str],
    subject: str,
    body_html: str,
    attachment_paths,
) -> bool:
    env = _read_smtp_env()
    # Environment variables take precedence over smtp.env so cloud deployments
    # (Railway, Heroku, etc.) can inject credentials without committing the file.
    host = os.environ.get("SMTP_HOST") or env.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or env.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or env.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS") or env.get("SMTP_PASS")
    if password:
        # Gmail app passwords are 16 chars often displayed with spaces — strip them.
        password = password.replace(" ", "")
    sender_addr = os.environ.get("SMTP_FROM") or env.get("SMTP_FROM") or user or ""
    sender_name = (os.environ.get("SMTP_FROM_NAME") or env.get("SMTP_FROM_NAME", "")).strip()
    if not (host and user and password and sender_addr):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_addr}>" if sender_name else sender_addr
    msg["To"] = ", ".join(to)
    msg.set_content("This message is HTML-only. Please use an HTML-capable mail client.")
    msg.add_alternative(body_html, subtype="html")
    for p in _normalize_attachments(attachment_paths):
        with open(p, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="pdf",
                filename=os.path.basename(p),
            )

    try:
        if int(port) == 465:
            # Implicit TLS (SMTPS)
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            # Opportunistic TLS via STARTTLS (Gmail default on 587)
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"[emailer] SMTP send failed: {e}", file=sys.stderr)
        return False


def send_alert(
    to: Iterable[str],
    subject: str,
    body_html: str,
    attachment_path=None,
    attachment_paths=None,
) -> bool:
    """Send the alert. Returns True on success.

    `attachment_paths` accepts a list of file paths; `attachment_path`
    is kept for backward compat (single string). Pass either, not both.

    Try SMTP first (Gmail or any SMTP server configured via smtp.env);
    fall back to Outlook COM on Windows if SMTP isn't configured.
    """
    paths = attachment_paths if attachment_paths is not None else attachment_path
    if _try_smtp(to, subject, body_html, paths):
        return True
    if _try_outlook_com(to, subject, body_html, paths):
        return True
    return False
