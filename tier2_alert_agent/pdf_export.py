"""
UAE Aid Agency — Tier-2+ Alert Agent
Dashboard-driven PDF export.

Renders the SAME executive summary PDF the dashboard produces for a given
country, by driving Microsoft Edge headlessly against the dashboard with
the `?printpdf=<country>&lang=<lang>` URL parameter (added to the dashboard
as an additive patch — see end of UAE_Humanitarian_Dashboard_Live.html).

Two pages, landscape, 1280×720 px each (cover + content slide), matching
the dashboard's brand template exactly.

DEPENDENCIES: Standard library only. Requires Microsoft Edge (preinstalled
on Windows 10/11). Falls back to Google Chrome if Edge isn't found.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
DASHBOARD = os.path.join(PARENT, "UAE_Humanitarian_Dashboard_Live.html")


def _find_browser() -> Optional[str]:
    """Locate msedge.exe (preferred) or chrome.exe. Returns absolute path or None."""
    candidates: List[str] = []
    # Common Edge install locations on Windows.
    program_files = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for base in program_files:
        if not base:
            continue
        candidates.extend([
            os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
        ])
    # PATH-resolved fallbacks.
    for name in ("msedge", "msedge.exe", "google-chrome", "chromium", "chrome", "chrome.exe"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def export_country_pdf(
    country: str,
    output_dir: str,
    lang: str = "en",
    timeout_sec: int = 60,
) -> str:
    """
    Render the dashboard's executive summary PDF for `country` and return
    the absolute path to the resulting PDF.

    Raises RuntimeError if the browser isn't found or the export fails.
    """
    if not os.path.exists(DASHBOARD):
        raise RuntimeError(f"Dashboard not found at {DASHBOARD}")
    browser = _find_browser()
    if not browser:
        raise RuntimeError(
            "Neither Microsoft Edge nor Google Chrome was found. "
            "Install Edge (default on Windows 10/11) and retry."
        )

    os.makedirs(output_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    safe_country = "".join(ch if ch.isalnum() else "_" for ch in country)
    out_path = os.path.join(output_dir, f"UAE_ExecSummary_{safe_country}_{today}.pdf")

    # Build the file:// URL with query parameters.
    dashboard_url = "file:///" + DASHBOARD.replace("\\", "/").lstrip("/")
    query = urllib.parse.urlencode({"printpdf": country, "lang": lang})
    target_url = f"{dashboard_url}?{query}"

    # Use an isolated user-data-dir so the headless run doesn't touch the
    # user's normal Edge profile.
    with tempfile.TemporaryDirectory(prefix="uaeaid_edge_") as user_data_dir:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--allow-file-access-from-files",
            f"--user-data-dir={user_data_dir}",
            f"--print-to-pdf={out_path}",
            "--no-pdf-header-footer",
            "--virtual-time-budget=20000",
            "--run-all-compositor-stages-before-draw",
            target_url,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Browser timed out after {timeout_sec}s while exporting PDF for {country}."
            ) from e

    if result.returncode != 0 and not os.path.exists(out_path):
        raise RuntimeError(
            f"Browser exited with code {result.returncode}. "
            f"stderr: {result.stderr.strip()[:500]}"
        )
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError(
            f"PDF was not generated (or is suspiciously small) at {out_path}. "
            "Verify the dashboard loads in a regular Edge tab with "
            f"?printpdf={country}&lang={lang} appended."
        )
    return out_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Export the dashboard's exec-summary PDF for a country.")
    p.add_argument("country", help='Country name as it appears in STATE.crises, e.g. "DR Congo".')
    p.add_argument("--lang", default="en", choices=["en", "ar"], help="Slide language.")
    p.add_argument("--out", default=os.path.join(HERE, "output"), help="Output directory.")
    args = p.parse_args()
    path = export_country_pdf(args.country, args.out, lang=args.lang)
    print("PDF:", path)
