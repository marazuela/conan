# Maintenance Session — 2026-04-16 08:50 UTC

**Session ID**: `maintenance-2026-04-16`
**Task**: `investment-tool-maintenance` (cron `50 */3 * * *`)
**Started**: 2026-04-16T08:50:00Z
**Prior session**: S66 operational (`scheduled-2026-04-16-0709`), released lock at 2026-04-16T08:15:30Z
**Next session**: Operational scanner (`0 */3 * * *`) ~2026-04-16 09:00 UTC

## Executive summary

**System health: EXCELLENT.** All structural checks passed. No bugs detected. No fixes applied. No warnings added. The system is ready for the next operational cycle.

---

## Phase 1 — Concurrency + orient

- Read SESSION_LOCK.md → `UNLOCKED` (last released 2026-04-16T08:15:30Z by S66).
- Acquired lock as `maintenance-2026-04-16` at 2026-04-16T08:50:00Z.
- Installed deps: requests, beautifulsoup4, lxml, yfinance, openpyxl, pandas — `yfinance-1.2.2` installed fresh; others already present.
- Read SESSION_STATE.md and confirmed active warnings list (34 items, including warning #34 EDGAR mna filter strengthening still pending — S67 priority #11).

## Phase 2a — Compile check (11 critical files)

All 11 critical tools compiled cleanly via `py_compile`:

| File | py_compile |
|------|-----------|
| edgar_filing_monitor.py | OK |
| esma_short_scanner.py | OK |
| congressional_trading.py | OK |
| contract_monitor.py | OK |
| fda_pdufa_pipeline.py | OK |
| convergence_engine.py | OK |
| mcap_cache.py | OK |
| run_scanner.py | OK |
| run_post_scan.py | OK |
| openfigi_resolver.py | OK |
| pipeline_runner.py | OK |

**Tail-integrity check** (anti-truncation, per S46 warning): all 11 files end with an `if __name__ == "__main__":` guard followed by a proper `main()` or `sys.exit(main())` call. No truncation observed.

## Phase 2b — `__pycache__` cleanup

- Deletion blocked by sandbox (warning #0l — known non-blocker).
- Inspected timestamps: all `.pyc` files dated 2026-04-16 08:00 UTC (last S66 operational run). Source files are coherent with these bytecode snapshots. No stale-override risk.

## Phase 2c — API reachability (7 endpoints)

| API | Status | Notes |
|-----|--------|-------|
| SEC EFTS | 200 (62,667 bytes) | Healthy |
| Capitol Trades `/trades?page=1` | 200 (table element present) | Healthy |
| USAspending spending_by_award (POST) | 200 (591 bytes) | Healthy |
| ClinicalTrials.gov v2 | 200 (20,447 bytes) | Healthy |
| openFDA drugsfda | 200 (4,200 bytes) | Healthy |
| FCA UK short-positions.xlsx | 200 (`application/vnd.openxmlformats…`) | Healthy; URL in `esma_short_scanner.py:44,64` matches |
| OpenFIGI v3/mapping (POST AAPL) | 200 (264 bytes) | Healthy |

All 7 sources reachable. No URL updates required. No transient retries needed.

## Phase 2d — Signals directory health

- Files >100 KB flagged for corruption inspection: 7 files, all confirmed legitimate (multi-item ESMA and congressional scan outputs with expected signal-schema keys: `company_name, isin, market_cap_mm, raw_data, scan_date, signal_category, signal_type, source_date, source_url, strength_estimate`). **No convergence feedback-loop corruption.**
- `convergence_engine.py` skip_keywords already includes `"convergence"` (line 160–161). Guard intact.
- Dedup logs within windows:
  - `congressional_dedup.json`: 611 entries, oldest 2026-04-09 (within 14-day window; cutoff 2026-04-02). 0 stale.
  - `contract_dedup.json`: 9 entries, oldest 2026-04-09 (within 30-day window; cutoff 2026-03-17). 0 stale.

## Phase 2e — SESSION_LOCK diagnostic

- Pre-acquisition state: `UNLOCKED`, last held by `scheduled-2026-04-16-0709` (S66), released 2026-04-16T08:15:30Z. State was clean — no stuck lock.
- Current state: held by `maintenance-2026-04-16` since 2026-04-16T08:50:00Z. Will be released at end of this session.

## Phase 3 — Signal quality audit

**`pdufa_watchlist.json`** (42 entries):
- Status distribution: `active=32, non_tradeable=5, approved=2, linked_to_TVTX=1, linked_to_GILD=1, resolved_crl=1`.
- **0 active entries with past PDUFA dates.**
- **0 duplicate `(ticker, drug_name)` pairs.**
- **0 non-US ticker suffixes (.ST/.TW/.L/.HK/.TO/.V/.PA/.F/.DE/.MX/.AX/.KS) missing `non_tradeable` tag.**

**`edgar_rotation_state.json`** — all 4 categories cycled within 12 hours (healthy):
- activist: 10.66h ago
- mna: 7.53h ago
- distress: 1.58h ago (most recent; was S66 operational scan)
- governance: 11.72h ago

No category >48h stale.

**Orphan signals**: 0 `_scanner_result_*.json` files older than 72h (5 total, all recent).

## Phase 4 — Bug detection + code quality

Scans performed:
1. **Tail-truncation** (S46 pattern): 11/11 files end with proper `__main__` guards. No truncation.
2. **Duplicate function/class definitions** (S46 pattern): 0 found across 14 tool files.
3. **Silent exception swallowing** (`except: pass` / `except Exception: pass` with no log/print): 0 found.
4. **Bare `except:` clauses**: 0 found.
5. **Hardcoded absolute paths** (`/sessions/...`, `/Users/...`, `C:\...`): 0 found.
6. **Unused imports** (cosmetic only):
   - `tools/congressional_trading.py:35` — `Any`
   - `tools/contract_monitor.py:28` — `re`
   - `tools/convergence_engine.py:33,34` — `timedelta`, `Tuple`, `Set`
   - `tools/edgar_filing_monitor.py:33,38` — `sys`, `Any`
   - `tools/esma_short_scanner.py:33` — `Optional`, `List`, `Dict`, `Tuple`
   - `tools/fda_pdufa_pipeline.py:33` — `Dict`, `Tuple`
   - `tools/google_trends_scanner.py:21` — `datetime`, `timedelta`
   - `tools/openfigi_resolver.py:37,38` — `field`, `Any`
   - `tools/uk_gazette_insolvency_scanner.py:29` — `re`

**Decision (not applied, documented)**: unused typing imports (`Dict, Tuple, Any, Set, List, Optional`) are low-risk but also low-value to remove, and maintenance task SCOPE LIMITS forbid aesthetic changes. Leaving as-is. The `re`, `sys`, `field`, and `datetime/timedelta` imports are also trivial and not causing runtime issues. No fixes applied.

**Warning #34 status** (EDGAR mna filter strengthening): not implemented yet in `edgar_filing_monitor.py` (grep for "Third Amended" / "Contribution Agreement" / "Item.*5\.02" / "item_subset" all returned zero hits). This remains S67 priority #11 — **out of scope for maintenance** (SCOPE LIMITS forbid adding strategies/filters without user approval). The operational task or a user-directed session must implement this.

## Fixes applied this session

**None.** The system is clean. No DECISIONS.md entry needed.

## Shutdown readiness

Context budget ample. All phases complete. Proceeding to Phase 5 shutdown:
1. Write this maintenance findings file ✅
2. No SESSION_STATE warning changes (nothing fixed, no new warnings)
3. Append PROGRESS_LOG entry
4. Update INDEX.md
5. Release SESSION_LOCK.md to `UNLOCKED`

---

# Maintenance Session — 2026-04-16 13:59 UTC (2nd slot)

**Session ID**: `maintenance-2026-04-16` (14:00 UTC slot)
**Task**: `investment-tool-maintenance` (cron `50 */3 * * *`)
**Started**: 2026-04-16T13:59:44Z
**Prior session**: Prior maintenance (11:50 UTC slot) released lock at 2026-04-16T11:55:00Z
**Next session**: Operational scanner (`0 */3 * * *`) ~2026-04-16 14:00 UTC

## Executive summary

System healthy. All 11 critical tools compile cleanly. All 7 external APIs return 200. pdufa_watchlist, edgar_rotation_state, dedup files all clean. No corrupt signal JSONs. No code changes made this session (no bugs found that were in scope to fix).

## Phase 1 — Orient

- SESSION_LOCK.md found UNLOCKED (released by prior 11:50 maintenance slot at 11:55 UTC). Lock acquired at 13:59:44Z.
- Python deps reinstalled: requests, beautifulsoup4, lxml, yfinance, openpyxl, pandas — OK.
- Read SESSION_STATE (S67 handoff, 11:00 UTC). Noted: no active blockers; warnings #0r, #20, #23 and S67 RPAY cross-check lesson carry over.

## Phase 2 — Structural Health

### 2a. py_compile on 11 critical tools

All 11 pass: edgar_filing_monitor, esma_short_scanner, congressional_trading, contract_monitor, fda_pdufa_pipeline, convergence_engine, mcap_cache, run_scanner, run_post_scan, openfigi_resolver, pipeline_runner. No truncation detected.

### 2b. __pycache__ cleanup

`rm -rf tools/__pycache__` returned **"Operation not permitted"** for all 14 .pyc files (same as the 08:50 UTC slot — this is the mount's read-only directory permission). However, py_compile in 2a **regenerated** all 11 critical .pyc files in-place at 14:00 UTC — individual file write is permitted, only unlink/rmdir is denied. Net effect: critical cache files are fresh. The 3 non-critical files (companies_house_monitor, google_trends_scanner, uk_gazette_insolvency_scanner) have older stamps but are not used by the operational pipeline. Persistent environmental quirk, not an operational issue. Logged to SESSION_STATE warnings.

### 2c. API reachability — ALL 7 HEALTHY

| API | Status | Notes |
|---|---|---|
| SEC EFTS | 200 | 62,674 bytes |
| Capitol Trades | 200 | HTML renders (243KB) |
| USAspending | 200 | 626 bytes |
| ClinicalTrials.gov v2 | 200 | 6 KB |
| openFDA (aspirin probe) | 200 | 4.2 KB |
| OpenFIGI (AAPL probe) | 200 | 264 bytes |
| FCA UK XLSX | 200 | 3.0 MB, correct content-type |

No endpoint updates needed.

### 2d. Signal directory health — CLEAN

- Large files (>100KB) are all expected (ESMA/AFM/BAFIN/FCA daily snapshots, congressional daily dumps). Not corruption.
- All `convergence_*.json` files are size 2 (`[]`). No feedback loop. Confirmed `skip_keywords` on convergence_engine.py line 160 includes "convergence".
- No zero-byte or malformed signal JSONs.
- Dedup window audit (today = 2026-04-16):
  - `congressional_dedup.json` (14-day window): 611 entries, oldest 2026-04-09, cutoff 2026-04-02 → 0 to prune (system live since Apr 9).
  - `contract_dedup.json` (30-day window): 9 entries, cutoff 2026-03-17 → 0 to prune.
- No `_scanner_result_*.json` older than 72 hours.

### 2e. SESSION_LOCK audit
Prior state UNLOCKED. Clean handoff from 11:50 slot.

## Phase 3 — Signal Quality Audit

### pdufa_watchlist.json — CLEAN
- 42 entries. Status distribution: 32 active, 5 non_tradeable, 2 approved, 1 resolved_crl, 1 linked_to_TVTX, 1 linked_to_GILD.
- **0** past PDUFA dates with `status="active"`.
- **0** duplicate `ticker+drug` pairs.
- **0** non-US ticker suffixes without `non_tradeable` tag.
- Near-term active PDUFA dates verified: AXSM 2026-04-30, ZLAB 2026-05-10, MNKD 2026-05-29, CING 2026-05-31, ARVN 2026-06-05.

### edgar_rotation_state.json — HEALTHY
All 4 categories scanned within the last 16 hours:
- activist: 2026-04-15T22:10:15Z
- mna: 2026-04-16T01:18:29Z
- distress: 2026-04-16T07:15:07Z
- governance: 2026-04-16T10:10:02Z (last_category)
No stall >48h.

### Orphan files
No `_scanner_result_*.json` older than 72h.

## Phase 4 — Bug Detection

- **AST syntax check**: 14/14 tool files parse cleanly.
- **Duplicate-tail check** (30-line window hash): no re-occurrence of the S46 truncation-duplication class of bug.
- **Hardcoded absolute paths**: 0 hits for `/sessions/`, `/home/`, `/Users/`, `C:\` in tools/*.py.
- **Silent try/except blocks**: 19 across 9 files. Inspected top occurrences in run_post_scan, esma_short_scanner, edgar_filing_monitor. All are narrow and intentional:
  - JSON-load fallbacks to empty defaults on missing/corrupt cache files.
  - `except ValueError: pass` on date parsing with safe defaults.
  - Report-building formatting fallbacks so partial data doesn't break report rendering.
  - `OSError` on per-file cleanup so one unremovable file doesn't abort the sweep.
  These are defensive patterns; modifying them would be behavior change and is out of scope per the maintenance task's "no architectural changes" rule.

**No code changes made this session.**

## Phase 5 — Shutdown

Proceeding with:
1. ✅ Write this findings section
2. Note the persistent __pycache__ unlink perm issue under SESSION_STATE warnings (same as 08:50 slot)
3. Append PROGRESS_LOG entry
4. Update INDEX.md (no new files; register this 14:00 slot in its entry)
5. Release SESSION_LOCK.md → UNLOCKED

---

# Maintenance Session — 2026-04-16 11:50 UTC (2nd run today)

**Session ID**: `maintenance-2026-04-16` (11:50 slot)
**Task**: `investment-tool-maintenance` (cron `50 */3 * * *`)
**Started**: 2026-04-16T11:50:00Z
**Prior session**: S67 operational, released lock at 2026-04-16T11:00:00Z
**Next session**: Operational scanner (`0 */3 * * *`) ~2026-04-16 12:00 UTC

## Executive summary

**System health: GREEN.** Identical clean state as 08:50 run — all 14 tools compile, all 7 APIs reachable, signals directory healthy, PDUFA watchlist/EDGAR rotation/dedup caches all within tolerances. Zero fixes applied.

## Phase 1 — Concurrency + orient

- Read SESSION_LOCK.md → `UNLOCKED` (last released 2026-04-16T11:00:00Z by S67 operational).
- Acquired lock as `maintenance-2026-04-16` at 2026-04-16T11:50:00Z.
- Installed deps via `pip install ... --break-system-packages`.
- Read SESSION_STATE.md (S67 handoff, 6 active candidates healthy, SEM newly on watchlist, no blockers) and INSTRUCTIONS.md.

## Phase 2a — Compile check (14 tools, expanded from 11)

All 14 .py files in `tools/` compile cleanly via `py_compile.compile(doraise=True)`:

```
OK: companies_house_monitor.py   OK: google_trends_scanner.py
OK: congressional_trading.py     OK: mcap_cache.py
OK: contract_monitor.py          OK: openfigi_resolver.py
OK: convergence_engine.py        OK: pipeline_runner.py
OK: edgar_filing_monitor.py      OK: run_post_scan.py
OK: esma_short_scanner.py        OK: run_scanner.py
OK: fda_pdufa_pipeline.py        OK: uk_gazette_insolvency_scanner.py
```

Tail-integrity: all 14 files have `if __name__ == "__main__":` guards (some at file end, some followed by body statements). No truncation.

## Phase 2b — `__pycache__` cleanup

Deletion failed (permission denied on Windows mount — 14 .pyc files locked). Inspected timestamps: all .pyc files dated 2026-04-16 11:00 UTC (regenerated during S67 operational). **.pyc and .py are in sync — no stale-override risk.** Non-blocker.

## Phase 2c — API reachability (7 endpoints)

| API | Status | Bytes |
|---|---|---|
| SEC EFTS | 200 | 62,672 |
| Capitol Trades | 200 (table present) | 243,872 |
| USAspending POST | 200 | 688 |
| ClinicalTrials.gov v2 | 200 | 6,077 |
| openFDA drugsfda | 200 | 4,200 |
| OpenFIGI v3 | 200 | 264 |
| FCA UK XLSX (HEAD) | 200 | 3,016,156 |

All 7 green. No URL or header changes.

## Phase 2d — Signals directory health

- 246 JSON files total in `signals/`.
- Files >100 KB: all legitimate (congressional dumps, ESMA short-interest full downloads, ESMA snapshots). No convergence feedback-loop corruption.
- **Convergence files >100 KB: 0.** Largest is 2,071 B.
- `tools/convergence_engine.py:160-161` skip_keywords includes `"convergence"` — guard confirmed intact.
- `congressional_dedup.json` 611 entries, oldest 2026-04-09 (within 14-day window — cutoff 2026-04-02). 0 stale.
- `contract_dedup.json` 9 entries, oldest 2026-04-09 (within 30-day window — cutoff 2026-03-17). 0 stale.

## Phase 2e — Lock state

Pre-acquisition: `UNLOCKED` from S67 clean release. No stuck-lock.

## Phase 3 — Signal quality audit

**`pdufa_watchlist.json`** (42 entries):
- Active entries with past PDUFA dates: **0** ✓
- Duplicate (ticker, drug_name) pairs: **0** ✓
- Foreign suffixes (.ST/.TW/.HK/.L/.PA/.DE/.MI) not tagged `non_tradeable`: **0** ✓
- Non-active kept for reference: 10 (TVTX approved, CORT approved, LGND linked, ACLX linked, PFMPY CRL-resolved, plus 5 non-tradeable foreign).
- Top of active list: AXSM (2026-04-30) — aligned with S68 T-14 kill-sweep cadence.

**`edgar_rotation_state.json`** — all 4 categories scanned within last ~13 hours:
- activist: 2026-04-15T22:10Z (~13h ago)
- mna: 2026-04-16T01:18Z (~10h ago)
- distress: 2026-04-16T07:15Z (~4h ago)
- governance: 2026-04-16T10:10Z (~2h ago, most recent)

Healthy rotation. No category >48h.

**Orphaned scanner results**: all 5 `_scanner_result_*.json` are 0.8h fresh. Zero orphans.

## Phase 4 — Bug detection + code quality

- Bare `except:`: **0**
- Silent swallows (except body = pass/continue/break only): **29 sites** across 9 files. Sampled 4 — all confirmed as legitimate fallback patterns (date-format chain, cache-corruption recovery, optional rendering, URL-discovery fallback). Not bugs.
- Hardcoded absolute paths: **0**
- Duplicate top-level function definitions (truncation signature): **0**
- Unused imports: ~19 occurrences (typing aliases, stdlib). Cosmetic. Left in place per scope limits.

**No fixes applied.** SCOPE LIMITS respected: no scanner runs, no candidate/scoring changes, no architectural edits.

## Shutdown readiness

Context budget sufficient. Proceeding to Phase 5:
1. Write findings ✅ (this append)
2. No SESSION_STATE warning changes
3. Append PROGRESS_LOG entry
4. Update INDEX.md if needed
5. Release SESSION_LOCK to UNLOCKED before 12:00Z operational cycle

