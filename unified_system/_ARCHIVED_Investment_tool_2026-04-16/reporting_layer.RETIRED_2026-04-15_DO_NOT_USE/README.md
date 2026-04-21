# Reporting Layer — Read-Only Analysis & Reporting

This subfolder contains automated and ad-hoc reports produced by scheduled tasks and sessions that **read** from the Investment Discovery System but **never write** to it.

## Architecture

Scheduled tasks that operate here:

1. **Performance Report** (`investment-tool-performance-report`) — Daily at 1:30 AM local. Produces an executive-style .docx summarizing tool health, candidate status, signal metrics, and system issues.

2. **Candidate Deep Dives** (`investment-tool-deep-dives`) — Every 4 hours. Scans for new/updated candidates, performs comprehensive web research, and produces per-candidate .docx deep dive documents.

Ad-hoc outputs (created by interactive sessions on request):

3. **Investment Theses** — Narrative, storytelling-style investment cases for presenting candidates to a decision-maker. Produced on-demand, not on a schedule.

## Read-Only Guarantee

All work here is architecturally constrained:
- READ from `investment_discovery_system/` (candidates, signals, reports, session state)
- WRITE exclusively to this `reporting_layer/` folder
- Never modify, move, rename, or delete any file in `investment_discovery_system/`
- Do not participate in `SESSION_LOCK.md` — zero risk of collision

## Folder Structure

```
reporting_layer/
├── README.md                           ← This file
│
├── deep_dives/                         ← Comprehensive per-candidate research (scheduled)
│   ├── index.json                      ← Dedup registry (ticker → score, catalyst, hash, doc_path)
│   ├── docx/
│   │   └── TICKER_Deep_Dive_YYYY-MM-DD.docx
│   └── pdf/
│       └── (PDF renders when requested)
│
├── investment_theses/                  ← Narrative investment cases (ad-hoc, on request)
│   ├── docx/
│   │   └── TICKER_Investment_Thesis_YYYY-MM-DD.docx
│   └── pdf/
│       └── TICKER_Investment_Thesis_YYYY-MM-DD.pdf
│
├── performance_reports/                ← Daily system-health reports (scheduled)
│   ├── docx/
│   │   └── System_Performance_YYYY-MM-DD.docx
│   └── pdf/
│
├── working/                            ← Scratch logs for tasks in-flight
│   └── *.log
│
└── archive/                            ← Superseded/cleanup artifacts (never deleted)
    └── YYYY-MM-DD_cleanup/
```

## File-Type Conventions

| Report Type | Naming | Audience | Length |
|---|---|---|---|
| Deep Dive | `TICKER_Deep_Dive_YYYY-MM-DD.docx` | Analyst (self) | ~8 pages, dense |
| Investment Thesis | `TICKER_Investment_Thesis_YYYY-MM-DD.docx` | Decision-maker | ~12 pages, narrative |
| Performance Report | `System_Performance_YYYY-MM-DD.docx` | Operator (self) | ~5 pages |

PDFs are generated on request from the matching .docx using LibreOffice (`scripts/office/soffice.py`). They live in the `pdf/` subfolder of their parent section.

## Schedules

| Task | Cron | Frequency | Avoids |
|------|------|-----------|--------|
| Performance Report | `30 1 * * *` | Daily 1:30 AM | Operational (XX:00), Maintenance (XX:50) |
| Deep Dives | `30 */4 * * *` | Every 4h at :30 | Operational (XX:00), Maintenance (XX:50) |

## Reorganization History

- **2026-04-13**: Restructured from a flat `candidate_deep_dives/` catch-all into document-type folders (`deep_dives/`, `investment_theses/`, `performance_reports/`) each with `docx/` and `pdf/` subfolders. Cleanup artifacts (lock files, LibreOffice temps, duplicate PDFs) moved to `archive/2026-04-13_cleanup/`. Rationale: separating document types by purpose scales better as the library grows and makes the thesis/deep-dive distinction visible at the folder level.
