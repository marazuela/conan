# Maintenance Report — 2026-04-16 05:00 UTC

Session: maintenance-2026-04-16 (cron 50 */3 * * * run, 04:59 UTC start)

## Phase 1: Concurrency + Orient
- SESSION_LOCK.md before: UNLOCKED (last: 2026-04-16T04:25:00Z, "completed").
- Lock acquired: LOCKED / 2026-04-16T04:59:35Z / maintenance-2026-04-16.
- Dependencies installed (`pip install requests beautifulsoup4 lxml yfinance openpyxl pandas --break-system-packages`). Package install tail showed warnings only (PATH notices), no errors.
- SESSION_STATE.md + INSTRUCTIONS.md read. S65 was last operational session; state is healthy.

## Phase 2: Structural Health

### 2a. Compile-check — ALL 14 tools pass
Critical 11 tools + 3 auxiliary: all `py_compile` PASS.
- edgar_filing_monitor.py (711L), esma_short_scanner.py (670L), congressional_trading.py (735L),
  contract_monitor.py (532L), fda_pdufa_pipeline.py (986L), convergence_engine.py (559L),
  mcap_cache.py (138L), run_scanner.py (185L), run_post_scan.py (412L),
  openfigi_resolver.py (547L), pipeline_runner.py (617L) — ALL OK.
- Auxiliary: companies_house_monitor.py (290L), google_trends_scanner.py (198L),
  uk_gazette_insolvency_scanner.py (217L) — ALL OK.
- Tail inspection: every file ends with its intended guard (`if __name__ == "__main__":`
  or equivalent). No truncation detected.

### 2b. __pycache__ cleanup
Known sandbox quirk: `rm -rf tools/__pycache__` returns `Operation not permitted` per file.
Non-blocking — sandbox re-imports will regenerate. Warning #0l in SESSION_STATE preserved.

### 2c. API reachability — 7/7 GREEN
All probes returned 200 with expected payload sizes/content-types:
- SEC EFTS            200 (62.5 KB)
- Capitol Trades      200 (HTML table present)
- USAspending         200
- ClinicalTrials.gov  200
- openFDA             200
- FCA UK (XLSX)       200 (application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)
- OpenFIGI            200

No URL changes or header fixes needed.

### 2d. Signals directory health
- Files > 100KB: all legitimate (ESMA aggregate files up to 1.58 MB, AFM/FCA/BAFIN snapshots,
  congressional signal files). NO corrupted convergence files (largest convergence_*.json = 2.1 KB).
- `convergence_engine.py` skip_keywords correctly includes `"convergence"` (line 160-161) plus
  `_scanner_result`, `dedup`, `watchlist`, `snapshot`, `rotation`, `cache`, `pdufa_watchlist`.
- Dedup logs — no stale entries beyond windows:
  - congressional_dedup.json: 611 entries, 0 stale (>14d)
  - contract_dedup.json: 9 entries, 0 stale (>30d)
  - edgar_dedup.json: 684 entries, 0 stale (>30d)
  - esma_dedup.json: 1691 entries, 0 stale (>30d)

### 2e. Lock state diagnostic
Lock was cleanly UNLOCKED by last session. No staleness, no orphans.

## Phase 3: Signal Quality Audit

### 3a. pdufa_watchlist.json (42 entries)
- Past PDUFA + active: **0** (previously-past entries correctly marked approved/CRL/etc.).
- Duplicate tickers: 3, all with **distinct drugs** (legitimate per protocol):
  - MNKD: Afrezza (May 29) + FUROSCIX ReadyFlow (Jul 26)
  - IONS: Olezarsen (Jun 30) + Zilganersen (Sep 22)
  - RARE: DTX401 (Aug 23) + UX111 (Sep 19)
- Non-US suffix tickers missing `non_tradeable`: **0**.

### 3b. edgar_rotation_state.json
All 4 categories scanned within 24 hours:
- activist: 2026-04-15T22:10:15Z
- mna: 2026-04-16T01:18:29Z (latest)
- distress: 2026-04-15T10:10:02Z
- governance: 2026-04-15T21:06:51Z

None stale. Rotation healthy.

### 3c. Orphan scanner result files
All 5 `_scanner_result_*.json` dated 2026-04-16 01:18–01:23 UTC (~3.7h old) — current, not orphans.

## Phase 4: Bug Detection / Code Quality

### Silent except-pass patterns
Enumerated across 10 files. Spot-checked convergence_engine.py:126
(_load_ticker_cache's `except Exception: pass`) — acceptable fallback
(returns empty dict if cache corrupt; non-critical enrichment path). The remaining
silent-except blocks are all in similar positions: date-parse fallbacks, optional
field extraction, OS-level cleanup. None mask hard errors.

### Improvement opportunity (logged, NOT fixed — out of scope)
Most silent `except ... pass` sites could be upgraded to `except ... : logger.debug(...)`
for observability. Not fixed this session to keep the maintenance scope tight — will
flag to OPEN_QUESTIONS if it becomes a diagnostic blocker.

### No duplicate-code / repeated tails
No repeated blocks detected; all tails end where designed.

## Phase 4c. Fixes made this session
**None.** System is in good health.

## Summary
- 14/14 tools compile clean.
- 7/7 external APIs reachable.
- 0 corrupted signal files.
- 0 stale dedup entries.
- 0 past-date active PDUFAs; duplicate tickers all legitimate.
- EDGAR rotation cycling normally across all 4 categories.
- No tool changes required.

Result: **CLEAN**. Operational scanner can proceed at 07:00 UTC.
