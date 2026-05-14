# UAE Aid Agency — Tier-2+ Alert Agent

An automated monitoring agent that emails Senior Leadership a one-page executive summary PDF whenever a humanitarian crisis moves to **Decision Tier 2 (Strong Response) or Tier 1 (Immediate Response)** under the framework defined in `UAE_Humanitarian_Monitoring_Methodology.md`.

Tier 1 is the most severe response level; Tier 2 is the next level down. The agent treats both as the "high-response zone" and alerts on every transition into or within that zone.

## What it does

Each run, the agent:

1. Loads the four data files in the parent dashboard folder:
   - `01_Active_Crises.csv` — severity, status, trigger
   - `02_Humanitarian_Indicators.csv` — PIN, funding %, access constraints
   - `03_UAE_Priority_Countries.csv` — strategic priority tier
   - `04_Response_Decision_Log.csv` — past engagement count (track-record bonus)
2. Computes each crisis's Priority Score (methodology §4) and maps it to a Decision Tier (§4.3).
3. Compares the current tier against the previous run's state (`state/last_tiers.json`).
4. For every crisis newly at Tier 1 or Tier 2, exports the **dashboard's own executive summary PDF** (brand template, 2-page landscape — cover + details slide) by driving headless Microsoft Edge against the dashboard with the URL parameter `?printpdf=<country>&lang=en`. Falls back to a reportlab-generated portrait PDF only if Edge isn't installed.
5. Emails the PDF to the recipients in `config.json`.
6. Writes a timestamped log line in `logs/`.

The first run seeds the state file and sends no alerts (to avoid an initial flood). Every run after that alerts only on **transitions** into Tier 1 or Tier 2.

## Files

```
tier2_alert_agent/
├── run_check.py        # Entry point — run this on a schedule
├── scoring.py          # Priority Score + Decision Tier formulae
├── pdf_export.py       # Drives headless Edge → dashboard PDF (primary)
├── exec_summary.py     # reportlab PDF generator (fallback)
├── emailer.py          # Gmail SMTP (primary) + Outlook COM (fallback)
├── config.json         # Recipient list, subject template
├── smtp.env            # REQUIRED — Gmail credentials live here
├── state/              # Auto-managed; do not edit
├── logs/               # Per-day run logs
├── output/             # Generated PDFs (one per alert)
└── preview/            # Static HTML preview of what the email looks like
```

### Dashboard patch

A small additive block was appended to `UAE_Humanitarian_Dashboard_Live.html` that activates only when the dashboard is opened with `?printpdf=<country>&lang=<lang>`. In that mode the dashboard, once data finishes loading, replaces its body with the printable brand-template page at exact 1280×720 dimensions. Normal dashboard usage is unaffected.

You can preview the exact PDF the agent will attach by opening the dashboard in your browser with that URL appended — e.g. `...UAE_Humanitarian_Dashboard_Live.html?printpdf=DR%20Congo&lang=en`.

## One-time setup (Windows)

```powershell
# 1. Make sure Python 3.9+ is installed and on PATH.
python --version

# 2. Install the one non-stdlib dependency (auto-installed on first run too).
python -m pip install --user reportlab

# 3. Set up Gmail SMTP credentials (see "Gmail setup" below).
#    Edit smtp.env with the app password you generated.

# 4. (Optional) Edit config.json to set the recipient list.
notepad "C:\Users\UAE-AID Agency\Desktop\Humanitarian Aid\Humanitarian Aid Monitoring System\tier2_alert_agent\config.json"

# 5. Smoke-test once (no email):
python "C:\Users\UAE-AID Agency\Desktop\Humanitarian Aid\Humanitarian Aid Monitoring System\tier2_alert_agent\run_check.py" --dry-run

# 6. Fire one test email to confirm Gmail send works end-to-end:
python "C:\Users\UAE-AID Agency\Desktop\Humanitarian Aid\Humanitarian Aid Monitoring System\tier2_alert_agent\run_check.py" --test-email
```

## Gmail setup

The agent sends via Gmail's SMTP server using an **App Password** (not your regular Google password). One-time:

1. Enable 2-Step Verification on the Google account that will send the alerts: https://myaccount.google.com/security
2. Create an App Password at https://myaccount.google.com/apppasswords — pick app *Mail*, device *Other (custom)*, name it *UAE Aid Alert Agent*. Google returns a 16-character code (e.g. `abcd efgh ijkl mnop`).
3. Open `smtp.env` next to `emailer.py` and fill in:
   - `SMTP_USER` and `SMTP_FROM` — the Gmail address that will send the alerts
   - `SMTP_PASS` — the 16-char app password (spaces are okay; they're stripped)
   - `SMTP_HOST` and `SMTP_PORT` are pre-filled with Gmail defaults

That's it — no Outlook required.

## Schedule it hourly with Windows Task Scheduler

Open Task Scheduler → Create Basic Task → Trigger: Daily, repeat every 1 hour → Action: Start a program

- Program/script: `python`
- Arguments: `"C:\Users\UAE-AID Agency\Desktop\Humanitarian Aid\Humanitarian Aid Monitoring System\tier2_alert_agent\run_check.py"`
- Start in (optional): `C:\Users\UAE-AID Agency\Desktop\Humanitarian Aid\Humanitarian Aid Monitoring System\tier2_alert_agent`

Run "whether user is logged on or not" so it fires even when the desktop is locked. SMTP works without anyone being signed in — that's the advantage over Outlook COM, which required an interactive session.

## Alternative: schedule it via Cowork

The agent can also be triggered by a Cowork scheduled task that runs Python on this folder hourly. The Windows Task Scheduler route is preferred because it runs even when Cowork is closed.

## Verifying it works

Force an alert without altering the data:

```powershell
python "...\tier2_alert_agent\run_check.py" --simulate C-013=2
```

This pretends Lebanon is at Tier 2 for that run and sends the alert. State is not modified during a simulation.

## Email sending

The agent sends via **Gmail SMTP** by default using the credentials in `smtp.env`. Works on any machine — no Outlook, no interactive session, no Office tenant admin required. The only setup is creating a Gmail App Password (one-time, takes 60 seconds — see Gmail setup above).

If `smtp.env` isn't filled in, the agent falls back to driving **Outlook desktop via COM** on Windows (if Outlook is installed and signed in).

## What "Tier 2" means here

Per methodology §4.3:

| Priority Score | Tier | Recommended UAE action |
|---|---|---|
| ≥75 | 1 — Immediate | Mobilize ERT; pledge within 7 days |
| **60–74** | **2 — Strong** | **Pledge within 14 days; multi-sector via UN / IFRC / INGO** |
| 45–59 | 3 — Targeted | Single-sector via pooled funds |
| 30–44 | 4 — Monitor | Diplomatic engagement |
| <30 | 5 — Track only | Weekly re-assessment |
