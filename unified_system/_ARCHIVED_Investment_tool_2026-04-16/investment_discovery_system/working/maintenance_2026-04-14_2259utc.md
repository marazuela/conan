# Maintenance Session — 2026-04-14 22:59 UTC

## Summary

Routine health check before operational cycle at 01:00 UTC 2026-04-15.
**Third file-truncation incident detected and repaired on `convergence_engine.py`.** A new and more dangerous variant of the bug was discovered.

## Phase 1 — Orientation

- Lock status at start: UNLOCKED (last released by S57 at 22:21 UTC). Stale check OK.
- Acquired lock at 22:59 UTC as `maintenance-2026-04-14`.
- Dependencies reinstalled (requests, beautifulsoup4, lxml, yfinance, openpyxl, pandas).
- Read SESSION_STATE — S57 finished clean, 14/14 compile OK, 11/12 APIs green last cycle.

## Phase 2 — Structural Health

### 2a — py_compile sweep
All 14 Python tools compile cleanly on initial sweep.

### 2b — pycache clear
**Permission denied** on `rm -rf tools/__pycache__/` (sandbox limitation — not a new issue). Worked around by force-recompiling via `py_compile.compile(..., doraise=True)` which rewrites the .pyc files in place.

### 2c — API reachability
All 7 endpoints GREEN:

| API | Status |
|-----|--------|
| SEC EFTS | 200 (62KB) |
| Capitol Trades | 200 (table present) |
| USAspending (POST) | 200 |
| ClinicalTrials.gov v2 | 200 |
| openFDA | 200 |
| FCA UK (XLSX) | 200 (3.0MB) |
| OpenFIGI v3 (POST) | 200 |

### 2d — signals/ directory health
- Files >100KB: all are legitimate raw signal dumps (ESMA positions list, congressional trade scrapes). No convergence files bloated — convergence filter is working as designed.
- `congressional_dedup.json`: 609 entries, all within 14-day window. No prune needed.
- `contract_dedup.json`: 8 entries, all within 30-day window. No prune needed.
- No orphaned/corrupted convergence files.

### 2e — Lock state
Confirmed UNLOCKED at entry. Normal.

## Phase 3 — Signal Quality Audit

### 3a — pdufa_watchlist.json (42 entries)
- Past-PDUFA still "active" & unresolved: **0**
- Duplicate (ticker, drug) rows: **0**
- Non-US tickers (XSPRAY.ST, 6446.TW): correctly tagged `status: "non_tradeable"` — clean.

### 3b — edgar_rotation_state.json
All 4 categories scanned within 14 hours (far under 48h threshold):
- activist: 12.8h ago
- mna: 6.9h ago
- distress: 0.9h ago (just scanned by S57)
- governance: 13.9h ago

`rotation_index: 2, last_category: distress` → next rotation should go to **governance** (index 3). Matches S57 expectation.

### 3c — orphaned _scanner_result_*.json
All 5 files from the current cycle (0.8h old). Normal.

## Phase 4 — Bug Detection + Fix

### CRITICAL FINDING — File truncation bug, 3rd occurrence, new variant

During the "file tail audit" step, `convergence_engine.py` was found truncated mid-word at `    report = gener` (expected tail: `    report = generate_report(convergences)\n    print(report)\n\n\nif __name__ == "__main__":\n    main()`).

**What makes this variant especially dangerous:**
- `py_compile` **passed** on the truncated file.
- `ast.parse` also passed.
- Reason: `report = gener` is a syntactically valid assignment of a name reference (`gener` is just an undefined identifier → would fail at runtime with `NameError`, but passes static syntax checks).
- **The maintenance protocol's current sole validation (`py_compile`) is insufficient to detect this class of truncation.**

**Fix performed:**
1. First attempted in-place completion via Python script → succeeded, file → 559 lines.
2. Then edited the file header to bump version to v1.4 via the Edit tool.
3. **The Edit-tool header write apparently re-truncated the tail** — post-edit inspection showed the file ending at `    pro` (now mid-word of `project_dir`), **543 lines, 20995 bytes**. This strongly suggests the Edit tool's write path is implicated in the truncation bug pattern — or at least is not reliable on this file.
4. Completed the fix a second time via atomic write using `tempfile.mkstemp` + `os.replace`. Final state: 559 lines, 21406 bytes, ends with proper `if __name__ == "__main__":\n    main()\n`.
5. Force-recompiled .pyc.
6. Full module execution via `importlib.util.spec_from_file_location` verified — `generate_report` and `run_convergence` both callable.

**Hypothesis on root cause:**
The file-truncation pattern has now occurred 3 times, all involving `convergence_engine.py`:
- S55-era first occurrence (historical)
- 2026-04-14 22:01 UTC maint (orphan-tail variant, 569 lines → truncated to 559, IndentationError)
- 2026-04-14 22:59 UTC this session (mid-statement variant, 553 lines, silent — py_compile passed)

A leading hypothesis is that the cowork file-write layer (Write/Edit tools) is writing to a OneDrive-synced Windows path that experiences partial-write corruption under specific conditions. The atomic-write-via-tempfile approach from bash avoided the recurrence. **Strong recommendation for future maintenance sessions: when repairing this file, use bash atomic-write (tempfile + os.replace) rather than the Write/Edit tools.** This is logged in DECISIONS.md as D-052.

**Recommended protocol enhancement (not implemented this session — scope limit):**
Add a second validation in the Phase 2a compile sweep that checks each tool's last non-blank line matches an expected terminal marker (e.g., `main()`, `sys.exit(main())`, `print("...")` for library-utility scripts). A mid-statement truncation at `report = gener` would fail this check even though py_compile passes. Proposed as Q-016 for user review.

### Other tools
No truncations or code-quality issues found in the other 13 tools. Tail audit clean.

## Phase 5 — Shutdown

Writing findings, updating SESSION_STATE (warnings updated for new variant), appending to PROGRESS_LOG, updating INDEX (new file), releasing lock.

## Files changed this session

- `tools/convergence_engine.py`: v1.3 → v1.4; tail restored (atomic write). 559 lines, 21406 bytes.
- `DECISIONS.md`: appended D-052 (atomic-write repair + root-cause hypothesis).
- `SESSION_STATE.md`: warnings updated — new truncation variant, py_compile insufficient, Edit-tool suspected.
- `PROGRESS_LOG.md`: appended maintenance entry.
- `INDEX.md`: new working/ file.
- `OPEN_QUESTIONS.md`: Q-016 drafted (tail-marker validation enhancement).
- `SESSION_LOCK.md`: released.

## Open questions raised for user

**Q-016** — Should the validation protocol add a "terminal marker" check alongside py_compile? Given that py_compile can't detect mid-statement truncation, a second line of defense seems prudent. Proposed implementation: a small checker that asserts the last non-blank line of each core tool matches an allowlist of terminal forms. Low-risk, high-value.
