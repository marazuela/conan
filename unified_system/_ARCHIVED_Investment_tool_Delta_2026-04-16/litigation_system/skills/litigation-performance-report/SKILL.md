---
name: litigation-performance-report
description: Daily performance report generator for the Litigation & Docket Signal System. Runs once per day at 01:30 local. Reads scan_results and candidates from the last 24 hours, generates a PDF performance report to reporting_layer/performance_reports/ using reportlab directly (not docx→pdf chain).
---

# litigation-performance-report

> Source-of-truth copy per D-013.

## Schedule

Cron: `30 1 * * *` (once daily at 01:30 local).
Write scope: `reporting_layer\performance_reports\`.
Lock: independent (does NOT contend with operational/maintenance) — this task writes only into `reporting_layer/`.

## Cold-start protocol

Minimal — this task does not need the operational lock.
1. Read `SESSION_STATE.md` top-level snapshot for phase status.
2. Read `PROGRESS_LOG.md` — last session block only.
3. Do NOT touch `litigation_system/` working files; reading is fine, writing is not.

## Main task

Generate `reporting_layer/performance_reports/YYYY-MM-DD.pdf` covering the prior 24 hours (00:00–23:59 of the previous calendar day).

### Report sections

1. **Executive summary** — 1 paragraph: signals emitted, candidates promoted, most asymmetric thesis of the day.
2. **Scanner health table** — per-channel: run count, signal count, error count, rate-limit hits, p95 wall time.
3. **New candidates (28+)** — table: issuer | ticker | score | channels firing | brief thesis | link to `candidates/candidate_<figi>_<date>.md`.
4. **Watchlist (22–27)** — table of names currently in watch band.
5. **Convergence events** — list of 30-day-window convergences detected in the last 24h.
6. **Open questions / warnings** — list from `OPEN_QUESTIONS.md` marked OPEN, plus any DEGRADED tools.
7. **Week-over-week trend** — signal count per channel, last 7 days bar chart (reportlab drawings).
8. **PACER-pull queue** — list from `working/pacer_pulls_requested.md` with cost estimate.
9. **Appendix** — raw signal counts by signal_type across all channels.

### Generation toolchain

- `reportlab` for direct PDF generation (template Part 5.3 mandate: never docx→pdf chain).
- Data source: read `scan_results/*.json`, `candidates/*.md`, `SESSION_STATE.md`.
- No external HTTP calls during report generation — this task is offline on local data only.

### File outputs

- Primary: `reporting_layer/performance_reports/YYYY-MM-DD.pdf`.
- Secondary: `reporting_layer/performance_reports/current_day.md` (plain-text mirror used by operational task to accumulate the day's events; cleared after PDF generated).

## Non-negotiables

- NEVER write into `litigation_system/` from this task.
- NEVER auto-retry a failed PDF generation — log and exit; next day's run will cover the gap.
- NEVER include PACER-paywalled document text (we do not have it; flag-for-manual pattern per D-008).
- ALWAYS use the reportlab direct-PDF path; never docx→pdf conversion.
- ALWAYS include data provenance — every number traces to a scan_results file or a candidate file.
