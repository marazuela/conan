---
name: litigation-operational
description: Primary autonomous operational task for the Litigation & Docket Signal System. Runs every 6 hours. Dispatches per-channel scanners according to their cadences, runs the 11-stage signal pipeline, updates candidates, and writes the daily report block.
---

# litigation-operational

> This is the SOURCE-OF-TRUTH copy. The scheduled-tasks MCP stores a deployed copy under its own path. Per D-013, edits here require re-deploy.

## Schedule

Cron: `0 */6 * * *` (every 6 hours at HH:00 local).
Write scope: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool Delta\litigation_system\` only. Candidate promotion to briefs is handled by the project-root `Reporting Hub/` (task `reporting-hub-deep-dives`), which reads this system's candidates and writes theses to `Reporting Hub/litigation_briefs/`. This system is producer-only and must not write into `Reporting Hub/`.
Lock: `litigation_system\SESSION_LOCK.md` — overwrite-only.

## Prime directive

Run the litigation signal pipeline autonomously. No chat output needed — artifacts are the output. Never ask the user questions; append to `OPEN_QUESTIONS.md` and keep going with unblocked work.

## Cold-start protocol (always run these first)

1. Read `SESSION_LOCK.md`. If LOCKED and < 4h old → exit cleanly. If LOCKED and stale (> 4h) OR UNLOCKED → overwrite to LOCKED with my session ID and timestamp, continue.
2. Read `SESSION_STATE.md` for TOP HEADLINE and priority queue.
3. Read `PROGRESS_LOG.md` — last 2 session blocks only (tail).
4. Read `INSTRUCTIONS.md` if any aspect of the architecture is uncertain.
5. Read `DECISIONS.md` only if a decision appears relevant to today's work.
6. Read `OPEN_QUESTIONS.md` — honor any blocking questions by working around them.

## Main loop (per session)

For each channel, check `working/<channel>_last_scan.txt` against its cadence (D-005):
- Federal Civil — if last scan ≥ 6h ago → dispatch `tools/pacer_recap_scanner.py`.
- ITC 337 — if last scan ≥ 12h ago → dispatch `tools/itc_337_scanner.py`.
- PTAB IPR — if last scan ≥ 24h ago → dispatch `tools/ptab_ipr_scanner.py`.
- Delaware Chancery — if last scan ≥ 12h ago → dispatch `tools/delaware_chancery_scanner.py`.
- SEC Enforcement — if last scan ≥ 6h ago → dispatch `tools/sec_enforcement_scanner.py`.
- DOJ/FTC Antitrust — if last scan ≥ 12h ago → dispatch `tools/doj_ftc_antitrust_scanner.py`.

For each raw signal produced:
1. **TRIAGE** — apply Stage 1 rules from the channel strategy. Drop obvious non-signals.
2. **PARTY-RESOLVE** — Stage 1 of CONTEXT.md two-stage protocol (strip suffixes, classify).
3. **ENTITY-RESOLVE** — Stage 2 (cache → EDGAR exact → EDGAR fuzzy → Exhibit 21 → OpenFIGI NAME).
4. **CONFIDENCE-GATE** — drop if `resolution_confidence < 0.85`.
5. **CONVERGE** — check 30-day window in `candidates/` keyed on `issuer_figi`.
6. **SCORE** — apply 7-dim rubric per `framework/scoring_system.md`.
7. **PROMOTE** — if score ≥ 28, create or update `candidates/candidate_<figi>_<YYYYMMDD>.md`.
8. **LOG** — every raw signal → `scan_results/<channel>_<YYYYMMDD>_<HHMM>.json`.

## Tool Health monitoring

For each scanner dispatch, measure: wall time, HTTP 200/non-200 counts, rate-limit hits (429), parse errors. Update `SESSION_STATE.md` Tool Health table.

## Daily report block

At the end of each run: append a one-paragraph block to `reporting_layer/performance_reports/current_day.md` with signals emitted, candidates promoted, unresolved counts. The full daily report is compiled by the separate `litigation-performance-report` task at 01:30 daily.

## Budget discipline

Per-scanner budget: 500 HTTP requests per pass. Hard timeout per scanner subprocess: 120s wall. Soft budget: 45s wall (pre-empt to next scanner if exceeded).

## Shutdown protocol (always run before exit)

1. Update `SESSION_STATE.md` — TOP HEADLINE, priority queue, Tool Health.
2. Append session block to `PROGRESS_LOG.md`.
3. Overwrite `SESSION_LOCK.md` to UNLOCKED with timestamp.
4. Do NOT delete anything. Archive as needed into `archive/YYYY-MM-DD_<reason>/`.

## Non-negotiables

- NEVER spend a PACER credit autonomously (D-008). If RECAP lacks a document, record `raw_data.document_status = "in_pacer_only"` and flag in `working/pacer_pulls_requested.md`.
- NEVER delete files.
- NEVER edit a settled decision in `DECISIONS.md`. Append a new numbered decision instead.
- NEVER ask the user questions during this scheduled run — append to `OPEN_QUESTIONS.md`.
- NEVER key convergence on a party-name string. Always on `issuer_figi`.
- ALWAYS set a User-Agent header on every HTTP call (required by SEC; courtesy for others): `"Litigation Signal Tool / Pedro (javiergorordo13@hotmail.com)"`.
