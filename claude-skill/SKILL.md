---
name: ibkr-update
description: Use when the user says "update", "refresh IBKR", "review this month", or asks about their IBKR account's returns, positions, trading style or risk. Reads the local Flex data, rebuilds ibkr_dashboard.html, and reports what changed and any risk flags.
---

# IBKR performance dashboard: update workflow

> Before using this skill, replace `<project path>` below with the real path on your computer, for example `C:\Users\you\Documents\ibkr-flex-performance`.

The user's IBKR data is pulled daily into a local folder by `pull_ibkr.ps1` (Windows Task Scheduler), and the dashboard is built by `analysis/build_dashboard.py`. This skill handles "update" and "review" requests.

## Folder

- Project path: `<project path>`
- If the folder isn't connected to the current session, request access to it first
- Find its mount point in bash, for example: `ls -d /sessions/*/mnt/<folder name>`

Key files: `data/pull_log.txt` (pull log), `data/latest.xml`, `data/raw/`, `data/benchmarks/`, `analysis/settings.json` (thresholds, token expiry), `ibkr_dashboard.html`, `reports/` (monthly snapshots). **Never read or display anything in `config/`, and never ask the user to paste their token into the chat.**

## Steps

1. **Check freshness**: read the last lines of `data/pull_log.txt` and find the latest `Success: A ~ B`. If B is earlier than the most recent completed US trading day, ask the user to double-click `run_pull.bat` and continue afterwards (or build with the existing data and state the as-of date). If the last entry is an `ERROR:`, use the error-code table in `docs/FLEX_SETUP.md`.
2. **Build the dashboard**:
   ```bash
   cd /tmp && python3 -I "<mount path>/analysis/build_dashboard.py" "<mount path>"
   ```
   The script prints a summary: NAV, the latest day's return vs. SPY, cumulative TWR, changes since the last update, closes on the latest day, open positions, risk flags, notable trading-style changes and new events.
3. **Report**: show `ibkr_dashboard.html` and summarize the main changes in a few sentences. Focus on **new or escalated risk flags** and **notable trading-style changes**; mention long-standing flags only briefly. Describe the data only: no buy/sell suggestions and no judgments about whether trades were good or bad.
4. **Monthly review**: when the user asks to review the month, add a 4–6 sentence recap: return vs. benchmarks, where the gains came from, the main losses, style changes and current risk points. Earlier months are available as snapshots in `reports/`.

## Key points

- The main measure is the adjusted TWR: temporary tax withholdings (`transactionTax`) are treated as cash temporarily leaving and returning, not as investment P&L. IBKR's own figure is shown alongside for comparison.
- Withdrawals don't affect the TWR. Simple "NAV ÷ total deposits" returns drop after a withdrawal; that is not an investment loss.
- If transaction tax has not been refunded (the "Transaction tax counted as a cost" flag), point it out: it may be a temporary withholding that will be refunded later (the return is corrected automatically when the refund appears) or a real cost such as stamp duty.
- To make the risk flags more or less sensitive, edit `thresholds` in `analysis/settings.json` and run step 2 again.
