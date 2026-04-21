# Decisions Log

*Record every meaningful decision with rationale and alternatives rejected. Newest entries at top.*

---

## D-052: Atomic-write repair of convergence_engine.py (3rd truncation, new silent variant) + Edit-tool suspected (2026-04-14, maint-22:59 cycle)

**Decision**: Repaired `tools/convergence_engine.py` via atomic-write (`tempfile.mkstemp` + `os.replace`) after two truncation incidents in this single maintenance session. Final state: 559 lines, 21406 bytes, tail ends with the correct `if __name__ == "__main__":\n    main()`. Bumped header v1.3 → v1.4 with note about py_compile-insufficient variant.

**Diagnosis — new bug variant**: On initial tail-audit the file ended mid-statement at `    report = gener`. Unlike prior occurrences (D-051: orphan-duplicate tail) this was a silent mid-statement cut that **passed both `py_compile` and `ast.parse`** because `report = gener` is a syntactically valid assignment of an undefined identifier. This means the existing maintenance protocol (py_compile as sole static gate) could NOT have caught this — only the tail-audit step surfaced it.

**Repair process (documented for future sessions)**:
1. First fix attempt: Python script rebuilt the tail from the Read-tool view (line 553 → 559). Verified with py_compile + importlib exec. Passed.
2. Second step: used the Edit tool to bump header from v1.3 to v1.4. **Post-edit inspection showed the file had re-truncated** to 543 lines / 20995 bytes, now ending mid-word at `    pro` (project_dir). Strongly suggests the Write/Edit tool write path is either a contributor to, or at least not resistant to, the truncation bug on this specific file (possible OneDrive sync interference).
3. Final fix: atomic write via bash using `tempfile.mkstemp` inside the same directory + `os.replace`. Post-write size 21406 bytes / 559 lines, tail verified byte-exact, module importable. Stable.

**Alternatives rejected**:
- Leave mid-statement-truncated file in place and flag only: unacceptable — operational scanner at 01:00 UTC would silently skip `generate_report()` → convergence output missing. Fix was in-scope per protocol 2a.
- Use Write tool for full file replacement: ruled out given the Edit-tool re-truncation observed mid-session; any cowork-layer write path is suspect for this file.
- Revert to prior git version: no git repo in workspace.

**Root cause hypothesis**: Three confirmed truncations on `convergence_engine.py` now (S55-era original, 22:01 UTC maint today, 22:59 UTC maint today). No other file has shown this. Working theory: OneDrive-synced Windows path introduces partial-write corruption under some timing condition, and this file's size (~21KB with long docstring header) may sit on a sync boundary that trips it. The atomic tempfile + rename approach appears to bypass whatever mechanism causes the truncation.

**Protocol gap identified**: `py_compile` alone is insufficient — it catches orphan-tail IndentationError (D-051 variant) but not mid-statement silent truncation (this variant). Proposing Q-016: add a terminal-marker assertion to the compile sweep. Low-risk one-screen change; big reliability win.

**Followups**:
- SESSION_STATE warning #1 updated — 3rd confirmed occurrence, new silent variant class added.
- SESSION_STATE warning #20 added — Edit/Write tools suspected on this file; prefer atomic bash writes for future repairs.
- Q-016 drafted in OPEN_QUESTIONS.md — add terminal-marker check to Phase 2a.

---

## D-051: Removed orphaned tail fragment from convergence_engine.py (2026-04-14, maint-22:01 cycle)

**Decision**: Truncated `tools/convergence_engine.py` from 569 lines to 559 lines to remove a 10-line orphaned tail fragment that caused `IndentationError: unexpected indent` at line 560. Bumped version header v1.2 → v1.3 with explanatory note.

**Diagnosis**: Lines 560-569 were a stray duplication of the `run_convergence(...)` call block and subsequent `generate_report(convergences)` / `print(report)` / `if __name__ == "__main__":` / `main()` that already existed correctly earlier at lines 548-559. The duplicate began with an over-indented `min_strategies=args.min_strategies,` (outside any function call) — classic signature of the file-truncation / duplicated-tail regression first documented in warning #1 (S46). This is the SECOND confirmed instance of the bug hitting convergence_engine.py across the last ~8 maintenance cycles.

**Impact**: The broken file would have caused the operational scanner's post-scan convergence phase (run_post_scan.py → convergence_engine.py) to fail on the next operational cycle. Fixed before operational S57 runs.

**Alternatives rejected**:
- Revert to a git version: No git repo in this workspace.
- Leave it broken and flag only: Violates maintenance mandate — I can fix compile-level breakage within scope (instruction 2a).
- Refactor the whole file: Out of scope — minimal change principle.

**Followup**: SESSION_STATE warning #1 remains open. A preventive measure (pre-shutdown py_compile sweep in operational sessions) is already in the S57 priority queue as item #5.

---

## D-050: Auto-Discover Dedup Hardening + Watchlist Corruption Guard (2026-04-13, S47)

**Decision**: Hardened `add_to_watchlist()` dedup logic and added watchlist corruption guard in `fda_pdufa_pipeline.py`.

**Changes**:
1. **Dedup fix**: When incoming `drug_name` is `"(auto-discovered)"`, dedup now matches on ticker alone (ignoring drug_name). This prevents auto-discover from adding duplicate entries when a curated entry exists with a real drug name. The dedup also blocks addition when the existing entry has any tracked status (`active`, `linked_to_*`, `resolved_crl`, `approved`), not just `active`.
2. **Corruption guard**: Before auto-discover saves, checks if the loaded watchlist has fewer than 20 entries (we maintain ~42). If so, logs a warning and skips saving auto-discovered entries to prevent overwriting a restored backup with corrupt data.

**Root cause**: A prior scheduled session's auto-discover feature overwrote the curated 42-entry watchlist with 22 bare-bones `"(auto-discovered)"` entries. The dedup check matched on `ticker + drug_name`, but `"(auto-discovered)"` never matched curated names like `"sparsentan (Filspari)"`.

**Alternatives rejected**:
- Disable auto-discover entirely: Loses the ability to find new PDUFA dates automatically.
- Match on ticker+date instead: Too narrow — same ticker could have different PDUFA dates for different drugs.

---

## D-048: Schedule Changed to Every 3 Hours, Every Day (2026-04-13, Interactive Session)

**Decision**: Changed operational scanner schedule from `0 9 * * 1-5` (once daily at 9 AM, weekdays only) to `0 */3 * * *` (every 3 hours, every day including weekends). Changed maintenance task from `0 6 * * 1-5` to `50 */3 * * *` (10 minutes before each operational cycle).

**Rationale**: User requested maximum scanning frequency including weekends. FDA can issue decisions on any day, and weekend regulatory news from ESMA regulators would otherwise be missed until Monday. Every-3-hours captures intraday developments.

**Alternatives rejected**: 
- Keep once-daily: Insufficient for catching intraday moves, especially around PDUFA dates.
- Every 3 hours weekdays only: Misses weekend FDA decisions and ESMA filings.

**Documentation updated**: INSTRUCTIONS.md (lock instructions, scheduled tasks section, version numbers), OPEN_QUESTIONS.md Q-004, SESSION_STATE.md.

---

## D-049: Comprehensive Bug Fix — 12 Audit Findings Resolved (2026-04-13, Interactive Session)

**Decision**: Resolved all 12 findings from the full system audit. Key fixes:
1. **ESMA scanner**: Removed duplicate trailing code (lines 671-679)
2. **EDGAR monitor**: Fixed `signals_dir` parameter bug in main(), deduplicated rotation logic to use `get_next_rotation_category()`, standardized date format to `datetime.now()`, bumped to v2.4
3. **Convergence engine**: Fixed file truncation (disk had 510 lines, needed 558), improved directional classification (added governance_keyword as bearish, contract_new_award/contract_modification/congressional_trade as bullish)
4. **PDUFA watchlist**: Tagged XSPRAY.ST, 6446.TW, EGTX as non_tradeable; tagged ACLX as linked_to_GILD
5. **run_scanner.py**: Corrected market cap help text from $300M to $215M
6. **run_post_scan.py**: Added convergence file cleanup (7-day retention)
7. **File truncations**: Discovered and fixed 4 truncated Python files (congressional_trading.py, contract_monitor.py, fda_pdufa_pipeline.py, convergence_engine.py) — all had missing CLI tails due to prior Write operations being cut short

**Root cause of truncations**: Write operations in prior sessions were interrupted or hit size limits, leaving files syntactically incomplete on disk while the Read tool's cache showed the full intended content. The discrepancy was only detectable by running `py_compile` in the bash sandbox.

---

## D-047: Daily Maintenance Scheduled Task Created (2026-04-13, Interactive Session)

**Decision**: Created a second scheduled Cowork task (`investment-tool-maintenance`) that runs daily at 6:00 AM local, Monday–Friday. Its sole purpose is system health checking, bug detection, and incremental improvement — it does NOT run the signal scanners or modify candidates/scoring.

**Concurrency**: Both tasks share the same `SESSION_LOCK.md` protocol. If either task finds the lock LOCKED and fresh (< 4 hours), it exits immediately without modifying any files. The maintenance task runs ~3 hours before the operational task (6:08 AM vs 9:07 AM), giving it a clear window to fix issues before the scanners need to run.

**Scope of maintenance task (4 phases)**:
1. Structural health: compile-check all tools, test API reachability, clear stale `__pycache__`, check for corrupted signal files, prune stale dedup entries
2. Signal quality audit: tally signals per scanner over last 3 days, flag dead/noisy scanners, verify convergence engine integrity, PDUFA watchlist hygiene, contract mapping gaps
3. Bug detection + auto-fix: file truncation, duplicate trailing code, OPEN_QUESTIONS review, anti-pattern scan
4. Shutdown: maintenance summary → update SESSION_STATE tool health → append PROGRESS_LOG → release lock

**Absolute constraints on maintenance task**:
- Never runs scanners (run_scanner.py, run_post_scan.py)
- Never modifies scoring logic, thresholds, or candidate files
- Never overwrites SESSION_STATE priority queue or candidate sections
- All fixes logged in DECISIONS.md before applying

**Alternatives considered**:
1. *Add maintenance to existing operational task* — rejected because the operational task already runs at capacity; adding diagnostic work would compete with scanning/scoring.
2. *Weekly instead of daily* — rejected because stale `.pyc` and API endpoint changes can silently break tools between sessions.
3. *No concurrency guard* — rejected; simultaneous writes to shared files (SESSION_STATE, signals/) would corrupt data.

**Task ID**: `investment-tool-maintenance`  
**Schedule**: `0 6 * * 1-5` (6:00 AM local, weekdays)  
**Lock identity**: `maintenance-[YYYY-MM-DD]` (vs `scheduled-[date]` for operational task)

---

## D-046: CORT Removed from PDUFA Watchlist — Already Approved (2026-04-13, Session 40)

**Decision**: Removed CORT (Corcept Therapeutics) from the active PDUFA candidate pipeline. Marked status as "approved" in `signals/pdufa_watchlist.json`.

**Discovery**: S40 web research revealed that FDA approved relacorilant (branded as **Lifyorli**) for platinum-resistant ovarian cancer on **March 25, 2026** — approximately 3.5 months ahead of the July 11, 2026 PDUFA target action date. The stock surged ~19.66% on approval day.

**Impact**: S39 had scored CORT at 27.25 as a PDUFA watchlist candidate. This score was inadvertently evaluating a binary event that had already resolved. The CORT entry in `session39_cort_preliminary.md` and `session39_pdufa_triage.md` are now historical artifacts.

**Root cause**: The FDA PDUFA pipeline tool relies on scheduled PDUFA dates and does not cross-reference the FDA approvals database. Early approvals (before PDUFA date) create stale entries.

**Remediation proposed**: Add an FDA approvals cross-check to the PDUFA pipeline tool — before outputting a signal, query the FDA approvals database to ensure the drug hasn't already been approved. Log as OPEN_QUESTIONS for future session implementation.

**Alternatives considered**: 
1. *Manual web research on every watchlist entry every session* — catches the issue but expensive in context tokens.
2. *Automated FDA approval cross-check in tool* — better long-term solution, requires implementation.

---

## D-045: Convergence Engine False-Positive Suppression List (2026-04-14, Session 38)

**Decision**: Added `CONVERGENCE_SUPPRESS` dictionary to `tools/convergence_engine.py` — a configurable suppression list for known false-positive convergences.

**First entry**: AMT (American Tower) — mega-cap REIT that generates routine congressional trade signals + frequent EDGAR filings, producing a false convergence alert every scan cycle since S33+.

**Rationale**: The convergence engine correctly detects AMT signals from 2+ strategies within the 14-day window, but these are noise — mega-cap names with high institutional/congressional trading volume will naturally produce multi-strategy signals that don't reflect genuine informational convergence. The suppression list is auditable (each entry has a reason string + session attribution) and logged (suppressed convergences appear in the log with a reason).

**Alternatives considered**:
1. *Market cap filter on convergence* — would suppress legitimate small-mid cap convergences. Too blunt.
2. *Recency-weighted convergence scoring* — more complex, addresses staleness but not the mega-cap noise problem.
3. *Manual review per session* — current approach. Works but wastes ~30 seconds of context per session identifying AMT as false positive again.

**Implementation**: Single dict lookup at start of entity loop in `detect_convergence()`. Zero performance impact. Logged at INFO level. Tested and verified S38 — "Detected 0 convergences (suppressed 1)".

---

## D-044: Market Cap Cache Module Created (2026-04-13, Session 36)

**Decision**: Created `tools/mcap_cache.py` — a shared cross-scanner market cap caching module with 24-hour TTL and file-based persistence.

**Rationale**: All 5 scanners independently call yfinance for market cap data, often for the same tickers (e.g., watchlist entries appear in PDUFA scanner and monitoring). Each yfinance call takes 2-5 seconds due to Yahoo Finance's rate limiting and cookie/crumb flow. A shared cache avoids redundant lookups. 24-hour TTL ensures data freshness while eliminating intra-day redundancy.

**Integration (completed S37)**: All 5 scanner tools' `_get_market_cap()` functions replaced with aliased import: `from tools.mcap_cache import get_market_cap_cached as _get_market_cap` (with try/except fallback for standalone execution). Old functions commented out. All tools compile and pass functional tests. Zero call-site changes needed due to alias approach.

**Alternatives considered**: (a) In-memory only cache — rejected because sandbox resets between sessions; file persistence means the cache survives within a session's multiple scanner runs. (b) Redis or similar — overkill for this use case. (c) Pre-populating cache at session start — viable addition, but the lazy-load approach works fine.

---

## D-043: EDGAR Keyword Scanner — Boilerplate Form Type Filter (2026-04-13, Session 36)

**Decision**: Added `KEYWORD_SKIP_FORMS` set to `edgar_filing_monitor.py` to filter out filing types that routinely contain boilerplate keyword matches. Forms skipped: ARS, DEF 14A, DEFA14A, DEFM14A, PRE 14A, N-CSR, N-CSRS, 497, 497K, NPORT-P.

**Rationale**: AMT was flagged as a convergence twice (S35, S36) due to an ARS filing containing "strategic alternatives" in routine corporate governance boilerplate. ARS (Annual Report to Shareholders) is not an activist filing — it's a glossy annual report. Similarly, DEF 14A proxy statements routinely contain governance language that triggers activist/governance keyword matches without being actual activist signals. Fund filings (N-CSR, 497, NPORT-P) are investment fund reports, not corporate actions.

**Alternatives considered**:
(a) Per-category form filters (activist skips ARS, but MNA doesn't) — more surgical but adds complexity. May revisit if the blanket filter suppresses real signals.
(b) Strength penalty instead of skip — rejected because boilerplate forms have zero informational value for our purposes.
(c) Filing type whitelist (only scan 8-K, 10-K, 10-Q, etc.) — too aggressive, may miss novel filing types.

**Impact**: Should eliminate false positives like AMT while preserving signal quality for genuine activist/distress/MNA filings in 8-K, 10-K, SC 13D, etc.

**Follow-up D-042**: This directly implements the improvement noted in D-042.

---

## D-040: PDUFA Scanner Disqualification Filter Implemented (2026-04-13, Session 35)

**Decision**: Added `DISQUALIFIED_TICKERS` dict to `fda_pdufa_pipeline.py` with ZLAB, CORT, ORCA. Signals from these tickers are now suppressed during `run_scan()`.

**Rationale**: These three tickers have been reappearing in every scanner run despite being disqualified in S34:
- ZLAB: Augtyro already FDA-approved (Jun 2024). Scanner picks up China NMPA milestones.
- CORT: Relacorilant (Lifyorli) approved Mar 25, 2026. Not a pending PDUFA.
- ORCA: Private company, not publicly traded.

**Alternatives considered**: (a) Remove from `pdufa_watchlist.json` — rejected because we want to preserve historical records. (b) Add a `disqualified` status field — viable but requires more code changes to the watchlist schema. The dict approach is simpler, explicit, and self-documenting.

**How to re-enable**: Remove the ticker from `DISQUALIFIED_TICKERS` and document in this file.

---

## D-041: Pipeline Triage — ARVN Added to Watchlist, ARQT/LNTH/IONS Discarded (2026-04-13, Session 35)

**Decision**: Of the four pipeline candidates flagged by S34, only ARVN survives to watchlist level (~22.5).

**Rationale**:
- ARVN (vepdegestrant, Jun 5): First PROTAC in oncology, Pfizer partner. Small cap ($695M), novel mechanism. Modest PFS data (5.0 vs 2.1 months). Worth monitoring — revisit at T-20.
- ARQT (Zoryve cream, Jun 29): Pediatric age expansion for approved drug. Routine, no edge. Score ~17. Discarded.
- LNTH (LNTH-2501, Jun 29): Diagnostic imaging kit with manufacturing delay. Not our strategy focus. Score ~18. Discarded.
- IONS (olezarsen, Jun 30): $12.4B large cap, label expansion, CEO discussing launch. Priced in. Score ~21. Discarded.

---

## D-042: AMT Convergence Alert Dismissed (2026-04-13, Session 35)

**Decision**: Dismiss AMT convergence alert as noise. No investigation warranted.

**Rationale**: Two independently low-quality signals coincidentally overlapping:
1. Congressional: Ro Khanna (D-CA) $8K child-account SELL. No committee alignment, sub-$15K, no predictive value.
2. EDGAR: "strategic alternatives" in ARS (Annual Report to Shareholders). Boilerplate language in corporate governance section, not an activist filing.

**Improvement noted**: EDGAR scanner should deprioritize ARS filings for activist keyword detection. ARS is the annual report, not an activist proxy or 13D.

---

## D-038: VRDN Score Reconciliation — S26 Confirmed, S33 Rejected (2026-04-13, Session 34)

**Decision**: Confirm VRDN at 26.0 (S26 score). Reject S33's suggestion of 30.0.

**Rationale**: S33 encountered the March 30, 2026 REVEAL-1 8-K during a PDUFA date verification task and interpreted it as "platform de-risking" suggesting 30.0. However, S33 did not have context that S26 had already performed a thorough analysis of the same event, including: (a) market expectations (51-73% placebo-adj responder rate expected, 36-45% delivered), (b) Tepezza competitive benchmark (83% responder rate), (c) the stock crash (-43.8% cumulative), and (d) franchise de-rating implications.

S26's analysis was correct and more thorough. The REVEAL-1 data was headline-positive but market-negative. S33's error was treating the 8-K as a fresh discovery without checking `working/` for prior analyses.

**Process improvement**: When scanners or verification tasks surface data from a press release or 8-K, ALWAYS check the `working/` folder for prior session analyses of the same event before writing a score recommendation.

**Alternatives rejected**:
1. *Average S26 and S33 scores* — arbitrary and unprincipled. One analysis was right, one was wrong.
2. *Accept S33's 30.0 on "most recent analysis wins" principle* — recency is not correctness. S26 did the deeper work.

---

## D-039: PDUFA Scanner False Positive Filter (2026-04-13, Session 34)

**Decision**: Add three additional filters to the FDA PDUFA scanner to reduce false positives.

**Rationale**: S33 surfaced three pipeline entries (ORCA, ZLAB, CORT) that all turned out to be non-actionable on S34 investigation:
- ORCA: Private company, not publicly traded (exchange "YHD" = delisted)
- ZLAB: Augtyro already FDA-approved June 2024; scanner picked up a China NMPA milestone
- CORT: Relacorilant (Lifyorli) approved early on March 25, 2026 (~4 months before PDUFA)

**Implementation**: Add to PDUFA scanner:
1. **Tradability filter**: Verify ticker is listed on a major US exchange (NYSE, NASDAQ, AMEX) via yfinance exchange field. Reject "YHD", "OTC", or empty exchange data.
2. **Prior-approval filter**: Check if the drug already has an FDA approval for the indicated condition via openFDA or Drugs@FDA.
3. **Geography filter**: For dual-listed companies (especially China ADRs), verify the PDUFA date refers to a US FDA action, not NMPA/EMA/MHRA.

**Alternatives rejected**:
1. *Manual review of all scanner output* — defeats the purpose of automation; these filters can be programmatic.
2. *Accept false positives as normal* — wastes session capacity on dead-end investigations.

---

## D-035: ESMA Same-Day Basket Short Signals Deprioritized (2026-04-10, Session 27)

**Decision**: When the ESMA scanner produces ≥3 new short position disclosures on the same calendar day across unrelated single names in a country/sector cluster (e.g., 4 French consumer/services names new on Apr 8 2026), treat the event as a **basket signal** and deprioritize individual names for candidate scoring.

**Rationale**: Session 27 ESMA scan produced 11 raw signals (6 unique entities) dominated by French names with new positions all dated 2026-04-08:
- UBI.PA (Ubisoft): 4 new holders Apr 8 (BlackRock, DE Shaw, Marshall Wace, Millennium)
- EDEN.PA (Edenred): 4 new holders Apr 8 (Marshall Wace, DE Shaw, AQR, Squarepoint)
- SW.PA (Sodexo): 1 new holder Apr 8 (Capital Fund Mgmt)
- VIRI.PA (Viridien): 1 new holder Apr 8 (Marshall Wace)
- ELIOR.PA (Elior): 1 new holder Apr 8 (Millennium Intl)
- ACCOR: 1 new holder Apr 8 (Ilex Capital)

Same day, same country, consumer/services tilt, same large L/S funds appearing across multiple names. This pattern is characteristic of **basket/pair trades** used to hedge existing long exposures, NOT of single-name specific information edge. When every L/S fund adds the same name on the same day, the "information asymmetry" collapses to zero — it is consensus crowding.

**Entry signal validation** compounded the conclusion: 4 of 6 names were RALLYING during the short build (UBI +7.5%, EDEN +21.5%, ELIOR +2.4%, ACCOR unresolved), which is a textbook thesis disconfirmation on entry. Only VIRI.PA (-10.1%) and SW.PA (-7.5%) had confirming price action, and neither scored above the 28 candidate floor after 7-dimension scoring (VIRI ~23-24, SW ~21-22).

**Implementation**:
1. When the scanner produces a same-day cluster of ≥3 new positions in one country or sector, the first analytical step is a **basket note** (what's the common denominator) before any individual scoring.
2. Only names that (a) show confirming price action AND (b) have independent information edge that survives the basket hypothesis should advance to full scoring.
3. Names in same-day clusters that still meet the bar should be scored with Information Asymmetry capped at 3.0 (they are by definition part of a crowded consensus).
4. The existing ESMA deduplication logic handles repeat signals; this rule handles basket concurrency.

**Alternatives rejected**:
1. *Score every name individually* — wastes session capacity. When basket dynamics dominate, the single-name signal-to-noise ratio is near zero.
2. *Archive all names in the basket unconditionally* — too blunt. Some names in a macro basket may still have genuine single-name catalysts.
3. *Only flag basket signals for manual review* — we can do both: flag AND score, with info asymmetry capped.

**Key lesson**: Pattern-matching at the scanner output level is as important as individual signal scoring. A 4-new-holder same-day same-country cluster is a higher-order signal about market positioning than any single name's short build.

---

## D-034: VRDN Demotion 31.5 → 26.0 Watchlist After REVEAL-1 Impact (2026-04-10, Session 26)

**Decision**: Demote VRDN (Viridian Therapeutics) from active candidate (31.5, post-Amgen-kill baseline) to **watchlist at 26.0** following adversarial review of price action since Session 25.

**Rationale**: Session 25 SESSION_STATE said "VRDN score 31.5 is already post-Amgen-kill. Do NOT re-score lower without new material information." Session 26 adversarial price check revealed two distinct crash events in the 8 sessions prior, cumulatively -43.8% from Mar 27 close:
- **Mar 30 2026**: -32.3% on 13.8M volume (10-14x avg) — REVEAL-1 elegrobart Phase 3 topline 8-K. Company called it "positive" but placebo-adjusted responder rates (36% Q4W / 45% Q8W) fell below investor expectations (51-73% range).
- **Apr 6 2026**: Additional -26.2% on 14.5M volume as investors digested the competitive implications. Elegrobart SC is not clearly superior to Tepezza SC (Amgen), erasing the long-term franchise extension moat.

Session 25 missed this by reading only the Apr 9 close as "stabilizing" and not pulling the intermediate history. The Apr 6 crash is unambiguously "new material information" that invalidated the S25 framing.

**Direct impact on June 30 veligrotug IV PDUFA**: None — REVEAL-1 is for elegrobart SC, a different molecule. Veligrotug IV Phase 3 THRIVE data is unchanged and approval probability remains ~75-80%.

**Indirect impact**: Franchise value is materially lower because the SC reformulation (the only weapon against Tepezza SC's market dominance) is weaker than expected. Commercial ramp post-approval is compromised. Information asymmetry has evaporated post-REVEAL-1 repricing.

**Revised 7-dimension score**: Signal Strength 3.0 (from 4.0), Catalyst Clarity 4.5 (unchanged), Info Asymmetry 2.0 (from 3.5), Risk/Reward 2.5 (from 3.5), Edge Decay 2.0 (from 3.0), Liquidity 4.0 (unchanged), Catalyst Timeline 4.0 (unchanged) → weighted **26.0** (from 31.5).

**Candidate file handling**: `candidates/VRDN_veligrotug_PDUFA.md` retained but tagged as demoted at top. Move VRDN entry from active candidates to watchlist in SESSION_STATE.

**Alternatives rejected**:
1. *Maintain 31.5* — Session 25 state said "do NOT re-score lower." But "do NOT without new material information" is a condition; the Apr 6 crash provides new material information.
2. *Archive entirely* — veligrotug IV PDUFA is still intact and approval probability is still reasonable; the thesis isn't broken, just weaker.
3. *Drop to below watchlist floor (21)* — too aggressive; still has a June hard catalyst with >50% approval probability.

**Kill conditions (watchlist)**:
- Price break >$19 on volume: re-investigate for re-elevation
- Price break <$12: archive entirely
- Any CRL pre-announcement or veligrotug-specific 8-K: instant archive

**Key lesson**: SESSION_STATE warnings like "do NOT re-score lower" are hypotheses to disprove, not rules. Every session must pull a full 10+ day price history for every active candidate, not just the current close. Any single-day move >5% automatically triggers a re-investigation memo. This is the fourth consecutive session-over-session correction (S23→S26); the adversarial review process is working, but session-to-session state propagation is systematically too sticky.

---

## D-033: AXSM Provisional Elevation 25.5 → 30.75 Candidate (2026-04-10, Session 25)

**Decision**: Elevate AXSM (Axsome Therapeutics) from watchlist (25.5, "mixed P3, asymmetry inverted") to **provisional active candidate at 30.75**. Full candidate file to be written next session after formal deep dive.

**Rationale**: The "mixed P3" framing inherited from prior sessions was a first-pass characterization that didn't survive scrutiny. The actual evidence base:
- **3 of 4 pivotal Phase 3 trials POSITIVE** (ADVANCE-1, ACCORD-1, ACCORD-2) with statistically significant CMAI improvements
- Only **ADVANCE-2 missed** primary endpoint (13.8 vs 12.6, 408 pts) — numerical improvement only
- **Priority Review granted** — FDA signal of favorable benefit-risk
- **Safety carryover from Auvelity/AXS-05 MDD approval (2022)** — large commercial safety database, no mortality or cognitive decline signals
- **Commercial preparation is aggressive**: doubling sales force 300→600 reps in anticipation, April 1 2026 M&A (balipodect PDE10A), Q1 earnings announced for May 4 (post-PDUFA) — NOT behavior of a company expecting a CRL
- **Jefferies $245 PT (Buy), Mizuho $230 PT (Outperform)** vs current $178.90 implies significant upside on approval
- **April 9 +5.6% on 2x volume** prompted the re-check — unusual pre-PDUFA accumulation consistent with institutional positioning

Revised 7-dimension score: Signal Strength 4.0 (from 3.5), Catalyst Clarity 4.5, Info Asymmetry 2.5, Risk/Reward 3.5, Edge Decay 3.0, Liquidity 4.0, Catalyst Timeline 4.0 → weighted **30.75** (from 25.5).

**Alternatives rejected**:
1. *Maintain 25.5 watchlist status* — stale framing, the 3-of-4 hit rate is materially different from "mixed"
2. *Elevate directly to 28+ active without provisional flag* — insufficient. Still need to verify no AdCom called, pull ADVANCE-2 full results, read openFDA safety, check option chain IV. Provisional flag preserves discipline.
3. *Wait for convergence* — unnecessary. The primary evidence stands alone.

**Kill conditions now monitored**: FDA AdCom announcement, price break <$165, new post-marketing safety signal, CRL/delay, sell-side downgrade <$200 PT.

**Next session**: Formal candidate file, option chain IV analysis, AdCom verification.

**Key lesson**: "Mixed" is a lazy descriptor that hides specific hit rates. Always quantify. 3-of-4 ≠ mixed; it is a strong majority.

---

## D-032: REPL Outcome Treated as Resolved Was Wrong — PDUFA is TODAY (2026-04-10, Session 25)

**Decision**: Retroactively correct the SESSION_STATE framing that REPL had received a July 2025 CRL as a recent event. REPL's **NEW PDUFA is April 10, 2026 — TODAY**. The July 2025 CRL was the original, which led to the Oct 2025 resubmission. **There is no 8-K from REPL in April 2026**; the binary is literally pending today.

**Rationale**: Session 24 correctly flagged web snippet evidence as "inconclusive" but failed to execute the recommended direct EDGAR 8-K pull to resolve. Session 25 executed the pull: REPL's last 8-K was Feb 3 2026 (earnings); zero filings in April. A CRL would trigger an 8-K within 4 business days by law. The absence of an 8-K confirms pending.

**Additional finding**: The 14 REPL Form 4s filed April 7 are **NOT a trading signal** — they are routine annual director/officer equity grants (transaction code A, disposition A, price $0, grant date 2026-04-01). Names include Weinand (director), Peeples-Dyer (director), Baker Bros Advisors (director, 2 grants), CFO Hill (2 grants), and 8 others. No bearish or bullish read. The only actual market-code transaction was CCO Sarchi selling 6,500 shares at $8.01 on Apr 2 (small, likely 10b5-1).

**Implications for TVTX**:
1. Monday PDUFA is NOT scientifically affected — different therapeutic class (sparsentan FSGS vs RP1 oncolytic HSV-1 melanoma combo)
2. **Sentiment overlay** is real and material for 48-72 hour window. REPL outcome will color biotech tape into Monday.
3. TVTX score unchanged at provisional 29.75 — underlying thesis intact.
4. Weekend positioning memo written (`working/session25_tvtx_weekend_positioning.md`) with 4 scenario paths.

**Alternatives rejected**:
1. *Treat REPL still as resolved* — factually wrong
2. *Treat TVTX and REPL as correlated risks* — scientifically wrong; different drugs, different therapeutic classes
3. *Add TVTX sentiment hedge* — we are not in a position-management role; our job is signal generation and kill-condition monitoring

**Process hardening**: Added protocol — when a referenced outcome is "inconclusive from web snippets," the NEXT session MUST resolve it via primary source (EDGAR/FDA) before any downstream analysis depends on it. An unresolved ambiguity cannot linger across sessions.

**Key lesson**: Session-to-session relay via SESSION_STATE is a strength for continuity but a weakness for error propagation. Working hypotheses inherit without re-verification. Need to flag hypotheses vs verified facts more explicitly.

---

## D-031: EDGAR Distress Rotation — SPAC/Pre-IPO Form Filter Required (2026-04-10, Session 22)

**Decision**: The EDGAR keyword rotation category `distress` requires a form whitelist and SPAC exclusion filter. Until implemented, distress-rotation signals must be treated as high-noise and deep-dived manually before scoring.

**Rationale**: Session 22 ran the distress category after D-030 deprioritized activist. Result: 30 raw signals → 28 dropped at mcap/strength pre-filter (CIK-only, unresolved, micro-cap) → 2 survived to deep dive (APAD, NUCL). **Both rejected as noise on inspection.**

- **APAD** (`AParadise Acquisition Corp.`, CIK 1956439, SIC 7990): triple-ticker SPAC (APAD/APADR/APADU) with an active S-4/A + Rule 425 stream for an Enhanced Ltd. business combination. "Going concern" language is standard SPAC-deadline risk-factor boilerplate in an S-4/A proxy, not an operating-distress disclosure.
- **NUCL** (`Eagle Nuclear Energy Corp.`, CIK 2089283, SIC 1090): warrant-paired (NUCL + NUCLW) pre-revenue uranium/nuclear company in the middle of an S-1 / S-1/A registration stream with multiple Form 3 initial-ownership filings. "Going concern" is standard pre-revenue S-1 risk factor language.

**Signal-to-noise: 0/30 = 0.0%** in distress category during the April SPAC/de-SPAC wave. This mirrors D-030's finding for activist during proxy season — the category is dominated by form-driven boilerplate, not genuine signals. The root cause: distress keywords ("going concern", "substantial doubt", "ability to continue") are **required disclosures** in S-1/S-4 risk factors for any blank-check or pre-revenue issuer, and there are dozens of such filings per week.

**Alternatives rejected**:
1. *Keep distress rotation as-is and rely on manual deep dive* — wastes session capacity. Two deep dives per rotation that always reject is a pure cost, no information gain.
2. *Drop distress category entirely* — over-reactive. Real distress signals (10-K auditor going-concern qualifications, 10-Q liquidity disclosures, 8-K Item 2.04/3.01 non-compliance notices) are exactly what the strategy is designed to catch. These are high-value.
3. *Add a SPAC name-string filter only* — insufficient. Catches name-obvious SPACs but misses de-SPAC companies that have renamed, and misses generic pre-revenue issuers whose boilerplate also triggers.

**Action** (tool fix queued as tool improvement; do NOT modify tool code during scheduled session without first logging):
1. **Form whitelist** for distress rotation: accept only `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `8-K` (Items 2.04, 3.01, 4.02, 5.02 ideally).
2. **Hard exclude forms**: S-1, S-1/A, S-4, S-4/A, 425, DRS, DRS/A, N-CSR (these produce the noise).
3. **Exclude SPAC tickers**: via OpenFIGI name match on `"Acquisition Corp"`, `"Blank Check"`, `"Acquisition Company"` — and exclude triple-ticker patterns (common/warrant/unit).
4. Until implemented, document this cost in PROGRESS_LOG and rotate next session to `m_and_a` rather than `distress`.

**Logged in**: `working/session22_edgar_distress_triage.md` (deep dive), `OPEN_QUESTIONS.md` (tool fix as Q-010).

---

## D-030: EDGAR Activist-Keyword Rotation Deprioritized During Proxy Season (2026-04-10, Session 21)

**Decision**: The EDGAR keyword rotation category `activist` (keywords: "strategic alternatives", "change in control", "maximize", "board", "activist", etc.) is deprioritized for the March–May proxy-season window. Next session will rotate to the next non-overlapping category (distress or M&A).

**Rationale**: Session 21 ran the activist category and returned 17 signals, of which **all 17 were noise**. Breakdown:
- 13 of 17 were DEF 14A / DEFA14A / ARS proxy statement filings from large companies (NOV, HSIC, MRK, PHIN, CALY, TRN, CSTL, STTK, etc.). The "change in control" and "strategic alternatives" language is CIC-severance and golden-parachute boilerplate required in every proxy statement under Item 402 of Regulation S-K. During the April annual-meeting wave, this produces enormous false-positive volume.
- 1 was CECO S-4 — a known, already-announced M&A registration statement (CECO/Thermon $2.2B deal announced Feb 24, 2026). Not a new discovery.
- 1 was AESI 8-K — routine Fifth ABL debt indenture amendment. The keyword match was inside financing boilerplate.
- 2 were ETF/fund noise (Next Bridge Hydrocarbons, FT 12871, NCA Nuveen).

**Signal-to-noise: 0/17 = 0.0%** in this category during proxy season. This is below the "rotate to different category" threshold. Governance (Session 19/20) also saturated at ~80 signals of which most were micro-cap proxy boilerplate — same root cause.

**Alternatives rejected**:
1. *Tighten activist filters (form whitelist + mcap floor)* — would help but doesn't address the core issue that DEF 14As are the dominant form and they all trigger the keywords. And filtering by form excludes the rare genuine 13D/activist announcement we want to catch.
2. *Drop the activist category entirely* — over-reactive. Outside of proxy season (roughly June–February), 13D amendments and genuine activist letters do surface here.
3. *Build a proxy-aware suppression filter* — expensive to implement correctly and risks suppressing genuine signals. Simpler to rotate the category during the known noise window.

**Action**:
- Next session: rotate to `distress` or `m_and_a` category (whichever comes next in `signals/edgar_rotation_state.json`).
- After proxy season ends (~mid-May), re-enable activist rotation but only on forms in {SC 13D, SC 13D/A, SC 14D9, PRER14A, DFAN14A} with a ≥$500M mcap floor.
- Log this filter change in INSTRUCTIONS.md when implemented.
- Record in OPEN_QUESTIONS.md as a tool-improvement item.

---

## D-029: ARVN Preliminary Score 28.0 — NOT Elevated to Candidate (2026-04-10, Interactive Fix Session continuation)

**Decision**: ARVN scored 28.0 preliminary (borderline candidate threshold). Not elevated to full candidate status. Held in research queue pending T-50 re-score with convergence check.

**Rationale**: VERITAC-2 ITT endpoint failed (p=0.07). Only ESR1m subgroup hit (HR 0.57, p<0.001). This caps the achievable label to ESR1m only (elacestrant precedent), which is largely priced in already. Stock down 86% from peak, 22 analysts cover, PT $17.80 = 60% upside. Binary catalyst in T-56 days, but base case is "approval with narrow label" = +15-30% reaction (not asymmetric). Signal Strength only 3/5 (single source — EDGAR auto-discovery only, no convergence).

**Alternatives rejected**:
1. *Elevate to candidate immediately* — Signal strength is weak (single source) and Info Asymmetry low (everything is priced in).
2. *Exclude entirely* — Score of 28 is at threshold; convergence bonus (e.g., Congressional buys, ESMA short covering) could push it over if new data emerges.
3. *Wait for full VERITAC-2 OS data* — OS is immature at topline, and may not read out before PDUFA.

**Action**: Re-score at T-50 (2026-04-17) OR immediately if: AdCom scheduled, Pfizer 8-K on commercial support, Arvinas capital raise, or market cap drops below $400M. Kill conditions documented in dossier.

---

## D-028: PDUFA Triage Exclusions — 10 of 13 New Auto-Discovered Entries (2026-04-10, Interactive Fix Session continuation)

**Decision**: Of 13 PDUFA entries auto-discovered from EDGAR 8-Ks, exclude 10 from scoring queue:
- **LGND**: Filspari royalty — same PDUFA as TVTX. Monitor via TVTX candidate.
- **CING ($70M), BTAI ($30M), UNCY ($170M), IRD ($390M)**: Below $215M mcap floor or fails asymmetry filter (presbyopia sNDA in crowded space).
- **AXSM ($9.15B), EXEL ($12.17B), SMMT ($14.97B), ZLAB ($2.34B), ACLX ($6.71B)**: Large-cap priced-in filter. ACLX specifically has Gilead acquisition pending ($7.8B) — no catalyst play possible.
- **CORT ($4.49B)**: Deferred — large cap + known CRL precedent on separate indication + standard (not priority) review.

**Elevated to scoring queue**: ARVN (pre-scored 28.0, T-50 scoring 2026-04-17), MLYS (T-60 scoring late Oct 2026).

**Rationale**: Auto-discovery is designed to find every PDUFA filing, not every investment opportunity. The triage layer applies investment filters (mcap floor, asymmetry, label risk) to surface the ~15-20% of entries that merit deep analysis.

**Alternatives rejected**: (1) Score all 13 (wastes 2-3 sessions on excluded names). (2) Exclude CORT too aggressively (its profitability + deep pipeline merits a second look at T-60). (3) Include AXSM (analysis confirms Session 18 exclusion was correct).

---

## D-027: MNKD Has Two Active PDUFAs — Both Added to Watchlist (2026-04-10, Interactive Fix Session continuation)

**Decision**: MannKind Corporation has TWO FDA decisions pending in 2026: **May 29** (Afrezza pediatric sBLA, existing watchlist entry) and **July 26** (FUROSCIX ReadyFlow autoinjector sNDA, newly added). Both added to watchlist as separate entries.

**Rationale**: Initial date mismatch detection (watchlist:May 29 vs. EDGAR auto-discovery:Jul 26) appeared to be a parsing error. Web research confirmed they are separate drugs: Afrezza (inhaled insulin expansion) and FUROSCIX (subcutaneous furosemide rapid-inject autoinjector). Auto-discovery was correct. The watchlist schema must support multiple PDUFAs per ticker.

**Alternatives rejected**: (1) Overwriting the May 29 entry with July 26 (loses the Afrezza catalyst). (2) Merging both PDUFAs into one record (harder to track kill conditions per-catalyst). (3) Treating July 26 as erroneous (contradicts verified source).

**Implication for VNDA**: Similarly, EDGAR auto-discovery found Feb 21, 2026 Bysanti PDUFA (already passed) and the existing Dec 12, 2026 imsidolimab entry. Both legitimate; Feb entry not re-added (past).

---

## D-026: FDA Pipeline v2.0 — EDGAR 8-K Auto-Discovery (2026-04-10, Interactive Fix Session)

**Decision**: Added automated PDUFA date discovery to `fda_pdufa_pipeline.py` (v1.0 → v2.0). Each scan searches EDGAR EFTS for recent 8-K filings containing "PDUFA" + "action date", fetches filing HTML, and extracts PDUFA dates via regex pattern matching. De-dups by ticker and updates existing entries if EDGAR has newer/different dates.

**Rationale**: Manual watchlist curation is a scalability bottleneck. Companies typically announce FDA acceptance (including PDUFA dates) in 8-K filings within 1-2 business days. ClinicalTrials.gov was explored as an alternative but returned empty results — "PDUFA" is not a clinical trials term. EDGAR is the authoritative source.

**Results**: Watchlist expanded from 28 to 41 entries (13 new discoveries + 1 date update). Key catches: LGND Apr 13 (royalty = linked to TVTX), AXSM Apr 30, ZLAB May 10, CING May 31, ARVN Jun 5, UNCY Jun 29, CORT Jul 11, IRD Oct 17, INO Oct 30, SMMT/BTAI Nov 14, EXEL Dec 3, MLYS Dec 22, ACLX Dec 23. Two date mismatches identified, both turned out to be legitimate dual-PDUFAs (see D-027).

**Alternatives rejected**: (1) Third-party API (FDATracker, BPIQ, BiopharmaWatch) — costs money, adds dependency, staleness risk. (2) Web scraping FDA calendar — FDA.gov doesn't publish PDUFA dates until approval. (3) Manual watchlist only — doesn't scale as pipeline grows.

---

## D-025: Session Lock Overwrite-Only Semantics (2026-04-10, Interactive Fix Session)

**Decision**: Changed SESSION_LOCK.md from create/delete to overwrite-only. Lock file always exists; first line is "LOCKED" or "UNLOCKED" + timestamp. Sessions check status field, not file existence.

**Rationale**: Cowork sandbox cannot delete files (`rm` returns "Operation not permitted"). The old create/delete pattern was fundamentally broken — sessions could create the lock but never release it. Overwrite-only works within sandbox constraints.

**Alternatives rejected**: (1) Using a separate API for locking (not available). (2) Relying on timestamp-only staleness (fragile — empty lock files can't be parsed).

---

## D-024: Convergence Engine Directional Filtering (2026-04-10, Interactive Fix Session)

**Decision**: Added directional classification to convergence engine (v1.1). Each signal is classified as bullish, bearish, or neutral based on signal_type. Convergences are labeled: bullish (all signals same direction), bearish, conflicting (opposing signals), or neutral. Conflicting convergences receive a 30% score penalty but are still flagged — the disagreement itself is informative.

**Rationale**: Without directional filtering, a short position increase + insider buy on the same entity would be flagged as confirming convergence, when they actually represent opposing views. This produced false positives.

**Alternatives rejected**: (1) Filtering out conflicting convergences entirely (loses valuable information — opposing signals on the same entity is often the most interesting finding). (2) Separate bullish/bearish convergence lists (more complex, less useful for ranking).

---

## D-023: ESMA Multi-Regulator Expansion (2026-04-10, Interactive Fix Session)

**Decision**: Expanded ESMA scanner from FCA-only (v1.0) to 4 regulators (v2.0): FCA (UK), AMF (France), AFM (Netherlands), BaFin (Germany). CONSOB (Italy) and CNMV (Spain) remain blocked.

**Key implementation details**:
- AMF: CSV via data.gouv.fr API; URL changes daily (timestamp in filename). Auto-discovery function queries API for current resource URL.
- AFM: CSV export; requires full browser User-Agent string (403 with truncated UA).
- BaFin: CSV download with session cookies via requests.Session().
- All regulators normalize to common position schema.

**Alternatives rejected**: (1) Browser automation for CONSOB/CNMV (higher complexity, Claude in Chrome dependency — deferred). (2) ESMA centralized API (doesn't exist for short positions). (3) Third-party aggregators (cost money, add dependency).

---

## D-022: Scoring Threshold and Market Cap Adjustments (2026-04-10, Interactive Fix Session)

**Decision**: (a) Lowered candidate threshold from 30+ to 28+. (b) Lowered market cap floor from $300M to $215M (€200M) across all tools except Contract Monitor ($300M retained).

**Rationale**: (a) With 5 strategies producing signals, more candidates should enter the deep dive pipeline. 28+ is still selective (66% of max score). (b) €200M is the minimum for EU mid-cap liquidity; converting at current EUR/USD gives ~$215M. This captures more European names from ESMA data.

**Alternatives rejected**: (1) Keeping $300M (misses EU mid-caps that are perfectly tradeable). (2) Lowering to $100M (genuine liquidity risk at that level). (3) Lowering candidate threshold to 25+ (too noisy — would flood the pipeline).

---

## D-021: Convergence Engine Ticker Enrichment + EDGAR MNA Strength (2026-04-10, Session 16)

**Decision**: Two improvements to the signal pipeline:

1. **Convergence engine ticker enrichment (convergence_engine.py v1.1)**: On signal load, enrich ISIN-only signals with tickers from `esma_ticker_cache.json`. Previously, 509 of 579 ESMA signals had no ticker and were grouped by ISIN, preventing cross-category convergence detection with US-scanner signals (which use tickers). After enrichment, all cached ISINs resolve to tickers, enabling proper entity grouping.

2. **EDGAR MNA strength differentiation (edgar_filing_monitor.py v2.4)**: Differentiate strength scoring for MNA signals by filing type. New deal announcements (8-K with "merger agreement") keep strength 5. Ongoing deal paperwork (SC TO-T, PREM14A, S-4, SC TO-C, etc.) scored at strength 2. New tender offers (SC TO-T, not amended) scored at 4. This prevents routine deal filings from drowning out genuine new signals.

**Alternatives rejected**:
- *Expand convergence window from 14 to 21 days*: Would catch ADM (19-day gap) but the signals are conflicting (congressional buy + ESMA short), not reinforcing. Keeping 14 days avoids false convergences.
- *Add intra-category convergence*: Considered detecting convergence within a single category (e.g., multiple ESMA holders shorting same stock). Deferred — the crowded short logic already captures this within the ESMA scanner.
- *Remove MNA signals entirely*: MNA filings are low-asymmetry by nature. Kept because (a) pre-announcement signals (unusual 13D before bid) remain valuable, and (b) differentiated strength lets downstream scoring filter correctly.

**Validation**: After enrichment, convergence engine detected ADM as 2-way convergence (congressional + esma_short) with 21-day window — confirms enrichment works. Default 14-day window correctly excludes this borderline case.

---

## D-019: EDGAR Keyword Category Rotation (2026-04-10, Session 16)

**Decision**: Implement category rotation for EDGAR keyword scanning. Each scan runs ONE category (activist → mna → distress → governance → repeat) plus filing-type searches, instead of attempting all 4 categories per scan.

**Rationale**: With the 35s wall-clock budget (D-018), only the first category ("activist") ever completes. Distress, M&A, and governance keywords were never scanned. Rotation ensures all 31 keywords get coverage across 4 consecutive scans.

**Alternatives considered**:
1. Increase wall-clock budget to 120s — Rejected: would exceed bash sandbox timeout (45s), causing sandbox lock (Q-007).
2. Run scanner in subprocess with longer timeout — Rejected: adds complexity, and subprocess within sandbox still risks lock.
3. Reduce keyword count per category — Rejected: loses signal coverage.
4. Use `--category` flag manually — Rejected: requires session to know which was last scanned.

**Implementation**: 
- Added rotation state file (`signals/edgar_rotation_state.json`)
- Added `--rotate` CLI flag to `edgar_filing_monitor.py` (v2.3) and `run_scanner.py` (v1.1)
- Updated `scheduled_task_prompt_v2.md` to use `--rotate` flag
- Rotation order prioritized: activist (highest hit rate), mna (time-sensitive), distress (contrarian), governance (corporate actions)

**Impact**: All categories covered every 4 hours with hourly scans. Max latency increase: 4 hours for non-priority categories. Acceptable given 24-48h scanning window.

---

## D-020: PDUFA Watchlist Expansion to Q3/Q4 2026 (2026-04-10, Session 16)

**Decision**: Expand `signals/pdufa_watchlist.json` from 10 entries (Apr 13–Jul 7) to 27 entries (Apr 13–Dec 23, 2026), adding 17 new entries sourced from CheckRare orphan drug calendar, company press releases, and web research.

**Rationale**: The watchlist endpoint (Jul 7) was approaching. Expanding to Dec 2026 ensures the FDA scanner has forward-looking data for triage and signal generation throughout the year.

**Key additions**: BMY (iberdomide, Aug 17), SVRA (molbreevi, Aug 22), CAPR (deramiocel, Aug 22), RARE (DTX401, Aug 23; UX111, Sep 19), NUVL (zidesamtinib, Sep 18), PRAX (relutrigine, Sep 27), INO (INO-3107, Oct 30), GILD (anito-cel, Dec 23).

**Priority triage for future scoring**: PRAX (study stopped early + first-in-class), RARE (two PDUFAs in 4 weeks), SVRA (first-and-only for rare disease), CAPR (resubmission binary).

**Full research notes**: `working/pdufa_expansion_q3q4_2026.md`

---

## D-018: EDGAR Filing Monitor v2.2 — Reduce Wall-Clock Budget to Prevent Sandbox Lock (2026-04-09, Session 15)

**Decision**: Reduce `WALL_CLOCK_BUDGET_S` from 90 to 35 seconds in `tools/edgar_filing_monitor.py` v2.2.

**Problem**: The EDGAR EFTS API is slow (often >45s for full keyword scan). When the scanner exceeded the 45s bash sandbox timeout, it locked the sandbox for ~3 minutes ("a]process is already running"). The scanner *did* write its results before being killed, but the locked sandbox blocked subsequent scanners and post-scan work.

**Fix**: Reduce internal wall-clock budget to 35s — enough time for the EDGAR scanner to complete most queries and gracefully save results, while leaving a 10s margin before the 45s bash hard-kill. The scanner already saves partial results when the budget is exceeded.

**Alternatives rejected**:
1. Increase bash timeout — not possible; 45s is the sandbox hard limit.
2. Split EDGAR scan into multiple bash calls (by keyword) — adds orchestration complexity; the scanner already prioritizes high-value keywords and saves partial results.
3. Run EDGAR asynchronously — subprocess isolation (D-014) already handles this; the issue is the scanner's own internal budget, not the orchestrator.

**Impact**: EDGAR may scan fewer keywords per run but will not lock the sandbox. Over multiple daily runs, all keywords will be covered.

---

## D-017: OpenFIGI Resolver v1.3 — Fix Batch Size for Unauthenticated Requests (2026-04-09, Session 15)

**Decision**: Reduce `MAX_BATCH_SIZE` from 100 to 10 in `tools/openfigi_resolver.py`. The OpenFIGI v3 API returns HTTP 413 when more than 10 mapping jobs are sent per request without an API key (100 is the limit *with* an API key). This caused the post-scan pipeline to fail entity resolution for all 88 signals.

**Alternatives rejected**:
1. Register for an API key — would raise limit to 100, but adds credential management complexity for a free service. Can revisit if 10-per-request becomes a bottleneck (currently 8 batches × 10 = 80 signals resolved in <7s).
2. Skip OpenFIGI for EDGAR signals that already have tickers — would lose canonical entity matching for convergence detection.

**Verification**: After fix, post-scan resolved 70/88 signals (up from 0/88). The 18 unresolved are likely entities without OpenFIGI matches (shell companies, very small caps, or ISINs without US listings).

---

## D-016: ESMA Scanner v1.1 — UK Ticker Resolution + Persistent Cache (2026-04-09, Session 14b)

**Decision**: Fix ISIN→ticker resolution for non-US equities by appending exchange suffixes (`.L` for GB, `.DE` for DE, etc.) after OpenFIGI lookup. Add persistent ticker cache (`signals/esma_ticker_cache.json`) that accumulates across scans, so the 25/min FIGI rate limit only applies to *new* ISINs. Raise FIGI_RATE_LIMIT from 20→25.

**Problem**: 555 of 570 ESMA signals had no market cap data. OpenFIGI returned bare tickers (e.g., `ABDN`) which yfinance couldn't resolve because LSE stocks need `ABDN.L`. Also, the 20-call FIGI limit meant only ~20 of ~180 entities got resolved per scan.

**Alternatives rejected**:
- Batch ISIN resolution via OpenFIGI: would require restructuring the scan loop; persistent cache achieves the same result over 2-3 scans
- Hardcoded UK ticker map: not scalable; new positions appear regularly

---

## D-015: Contract Monitor v1.1 — Sort by Start Date + Stale Filtering (2026-04-09, Session 14b)

**Decision**: Change USAspending API query to sort by `Start Date desc` (was `Award Amount desc`) and post-filter awards whose `Start Date` is more than 180 days old. Also widen `DAYS_BACK` from 2→7.

**Problem**: Sorting by Award Amount surfaced massive legacy contracts (e.g., $34B DOE contract from 2018, $17B Honeywell from 2015) that had recent modifications. These produced stale signals (dates from 2019-2022) that were not actionable.

**Alternatives rejected**:
- Different API endpoint (award transactions): more complex, unnecessary — sorting by Start Date solves it
- Stricter time_period filter: the API's time_period already filters on action_date; the issue is what results are surfaced

---

## D-014: Subprocess Isolation for Scanner Execution (2026-04-09, Session 14)

**Decision**: Rewrite `pipeline_runner.py` `run_scanner()` to execute each scanner in a separate subprocess with a hard timeout (120s default, configurable via `--timeout`).

**Rationale**: During the first integrated scan attempt, the EDGAR scanner hung on a slow SEC API response. Because scanners ran in-process, the hung call blocked the entire bash sandbox for 10+ minutes, preventing all work. This is a critical reliability issue for scheduled sessions that must complete autonomously.

**How it works**: Each scanner runs as `subprocess.run([python, "-c", script], timeout=SCANNER_TIMEOUT_S)`. The script imports the scanner module, runs its entry point, and writes signals to a temp JSON file. If the subprocess exceeds the timeout, it is killed (`TimeoutExpired`), the pipeline logs the failure, and continues to the next scanner. Signals are read from the temp file on success.

**Alternatives rejected**:
- `signal.alarm()` (Unix only): Works but kills the entire process on timeout, not just the scanner. Can't recover gracefully.
- `multiprocessing.Process` with timeout: More complex, harder to debug, signal serialization issues. subprocess is simpler and the signal JSON file approach is robust.
- In-process with `requests.timeout`: Only catches HTTP-level timeouts; doesn't handle hangs in yfinance, BeautifulSoup parsing, or other non-HTTP operations.

**Impact**: pipeline_runner.py v1.0 → v1.1. Same external interface. CLI gains `--timeout` argument.

---

## D-013: Capitol Trades Replaces Quiver Quantitative (2026-04-09, Session 11)

**Decision**: Use Capitol Trades (capitoltrades.com/trades) as the primary data source for congressional trading, replacing Quiver Quantitative.

**Rationale**: Quiver Quantitative API now returns HTTP 401 ("Authentication credentials were not provided") — the strategy spec said "free, no auth" but this has changed. Capitol Trades provides comparable data: politician name, party, chamber, state, ticker, trade type, size range, dates. Free, no auth, structured HTML table.

**Alternatives rejected**:
- Wait for Quiver API key: Unknown cost, delays build. Capitol Trades provides equivalent data now.
- House Clerk PTR search: Less structured, harder to parse, no ticker resolution.
- OpenSecrets: Requires API key, different data format.

**Impact**: Congressional trading strategy spec (`strategies/congressional_trading.md`) still references Quiver as original plan. Tool code uses Capitol Trades. No data quality loss — both sources derive from the same STOCK Act filings.

---

## D-012: File-Based Concurrency Lock for Scheduled Sessions (2026-04-09)

**Decision**: Use a `SESSION_LOCK.md` file as a concurrency mechanism to prevent multiple scheduled sessions from running simultaneously.

**Why**: Scheduled sessions run hourly, but each session may use the full usage window (up to 5 hours). Without a lock, a new hourly trigger would start a second session while the first is still active — both writing to the same files (SESSION_STATE, PROGRESS_LOG, tools, candidates), causing state corruption.

**How it works**:
- At startup: check for SESSION_LOCK.md. If absent → create it with timestamp, proceed. If present and <4 hours old → stop immediately (another session is active). If present and >4 hours old → assume crash, override the lock, proceed.
- At shutdown: delete SESSION_LOCK.md as the final step, releasing the lock for the next hourly trigger.
- Interactive sessions with Pedro present skip the lock check.
- The 4-hour staleness threshold prevents permanent deadlock from crashed sessions while leaving enough margin for long-running sessions.

**Alternatives rejected**:
- No lock (just hourly triggers) — concurrent writes would corrupt project state
- Manual/on-demand scheduling only — defeats autonomous operation
- Database or external lock service — overengineered for a file-based project

---

## D-011: Web Research Layer Added to Deep Dive Analysis (2026-04-09)

**Decision**: Add a mandatory web research layer to every candidate deep dive (30+ scores and Watchlist). This layer uses WebSearch and WebFetch to gather recent news, analyst activity, litigation, regulatory actions, social sentiment, and market narrative context.

**Why**: Our signal sources are structured data — filings, disclosures, contracts, regulatory registers. They tell us *what happened* but not *what the market narrative is*. A company could have a strong signal (e.g., activist 13D filing) but the market may already be pricing in the catalyst because of news coverage we wouldn't see in the structured data. Conversely, web research might reveal a kill condition (pending lawsuit, regulatory investigation) that doesn't appear in our signal sources. The web research layer bridges this gap.

**Where it sits in the pipeline**: After strategy-specific analysis, before the candidate writeup is finalized. It is a validation and enrichment step — it can strengthen a thesis, weaken it, or reveal a kill condition.

**Alternatives rejected**:
- Making web research optional — too risky; a candidate without narrative context is a blind spot
- Running web research only for convergence candidates — misses single-strategy candidates where news context could be the difference between a good trade and a bad one
- Building an automated news scraping tool — overkill for now; WebSearch provides real-time results without building/maintaining infrastructure

**How to apply**: The full research checklist is in `framework/scoring_system.md` (Stage 3 section) and the output template is in `framework/candidate_template.md` (Web Research section). Every strategy spec's deep dive checklist also references this layer.

---

## D-010: PDUFA Calendar — Semi-Manual Approach (2026-04-09)

**Decision**: Track upcoming PDUFA dates via WebSearch + manually curated JSON watchlist, not automated scraping.

**Why**: No clean PDUFA calendar API exists. FDA.gov 2026 novel drug approval pages return 404 (may not be published yet). biopharmawatch.com is JavaScript-rendered — no table data in the HTML source. fdatracker.com not tested but likely similar.

**Alternatives rejected**:
- Browser automation (Claude in Chrome) to scrape JS-rendered sites — adds complexity and fragility for a calendar that only needs updating weekly
- Waiting for FDA.gov to publish 2026 pages — uncertain timeline, blocks the entire strategy

**How to apply**: The FDA tool will use ClinicalTrials.gov API + openFDA API for automated data (trial results, approval history). PDUFA date tracking is the one component across all 5 strategies that requires periodic manual/WebSearch input. This is acceptable — the dates change infrequently and the rest of the analysis chain is fully automated.

---

## D-009: Committee Cross-Reference — Static Lookup Table (2026-04-09)

**Decision**: Build a static JSON lookup table mapping BioGuideID → committee assignments, instead of querying congress.gov API in real time.

**Why**: congress.gov API is slow, XML-only, unreliable for bulk lookups, and rate-limited with DEMO_KEY. A static table is instant, reliable, and only needs updating when a new Congress is seated (every 2 years).

**Alternatives rejected**:
- Real-time congress.gov API calls per trade — too slow, unreliable, XML parsing overhead
- Scraping committee membership pages — fragile, no better than maintaining a table

---

## D-008: yfinance for Market Cap Triage (2026-04-09)

**Decision**: Use the `yfinance` Python library (not Yahoo Finance REST APIs) for all market cap, volume, and revenue lookups.

**Why**: Yahoo Finance v7 and v10 REST APIs now return 401 Unauthorized — they require authentication as of 2026. The yfinance library works without auth for both US stocks and UK stocks (.L suffix). Verified: AAPL returns $3.8T market cap, ABDN.L returns £3.5B.

**Alternatives rejected**:
- Yahoo Finance REST APIs with auth — would need API key registration, adds dependency
- Financial Modeling Prep API — requires paid key for market cap
- Manual market cap lookup — defeats autonomous operation

---

## D-007: USAspending.gov Replaces SAM.gov (2026-04-09)

**Decision**: Use USAspending.gov API as the primary data source for government contract awards, replacing SAM.gov.

**Why**: SAM.gov API requires a free API key with 1–4 week approval wait, has a 10 req/day limit, and was blocked from the Cowork sandbox during testing. USAspending.gov provides the same contract award data with zero setup — no API key, no registration, no rate limit at our usage levels. Verified accessible with live queries returning award data filtered by amount, date, and recipient.

**Alternatives rejected**:
- SAM.gov API — 1-4 week key wait is the biggest calendar blocker across all strategies; eliminates it entirely
- FPDS Atom feed — decommissioned February 2026
- USAspending `recipient_search_text` filter — tested, times out; use contractor mapping table instead

---

## D-006: Quiver Quantitative Replaces Lambda Finance (2026-04-09)

**Decision**: Use Quiver Quantitative API for congressional trading data, replacing Lambda Finance.

**Why**: Lambda Finance API DNS is blocked from the Cowork sandbox (cannot resolve hostname). Quiver Quantitative is accessible, returns 1,000 trades per call with richer data — includes ExcessReturn, PriceChange, and SPYChange fields that enable legislator track record analysis without additional calculations.

**Alternatives rejected**:
- Lambda Finance API — DNS blocked, would need VPN or alternative network path
- Capitol Trades HTML scraping — accessible (200 OK) but fragile, less data, no return metrics
- Raw House/Senate disclosure sites — Senate returns 403, House is accessible but raw PDFs

---

## D-005: ESMA Phased by Data Accessibility (2026-04-09)

**Decision**: Implement ESMA short position aggregation in phases ordered by data accessibility: Phase 1 (FCA UK), Phase 2 (Bundesanzeiger Germany), Phase 3 (CNMV Spain).

**Why**: FCA provides a clean XLSX file with 579 positions — zero parsing complexity, verified downloadable. Bundesanzeiger loads but uses dynamic rendering — needs investigation. CNMV returns 403 Forbidden from the sandbox — requires browser automation.

**Alternatives rejected**:
- Starting with CNMV (Pedro's home market) — technically hardest, 403 blocker
- Waiting for all regulators before launching — delays the strategy unnecessarily; FCA alone provides valuable UK short position data
- Original BaFin-first ordering — BaFin delegates to Bundesanzeiger which is harder to parse than FCA

---

## D-004: Hybrid Architecture — Python Tools + Claude Sessions (2026-04-09)

**Decision**: Data collection via Python tools; analysis, scoring, and reporting via Claude Cowork sessions.

**Why**: Python tools are better at repetitive API calls, rate limiting, JSON parsing, and file I/O. Claude sessions are better at reading filings, scoring signals with judgment, writing candidate analyses, and producing daily reports. Hybrid plays to each layer's strengths.

**Alternatives rejected**:
- Pure Claude session (no Python) — too slow for API calls, can't handle rate limiting or parallel requests efficiently
- Pure Python tool — loses Claude's analytical capabilities for deep dives, scoring, report generation
- External infrastructure (AWS Lambda, cron jobs) — adds operational complexity Pedro doesn't need; Cowork scheduled sessions handle both layers

---

## D-003: 7-Dimension Scoring System (2026-04-09)

**Decision**: Score signals on 7 weighted dimensions (max 42.5) with convergence bonus (up to +8). Thresholds: 30+ Immediate, 22-29 Watchlist, 14-21 Archive, <14 Discard.

**Why**: Original 5-dimension system missed Liquidity and Catalyst Timeline, both of which materially affect position viability. Liquidity determines whether a signal is tradeable at size. Catalyst Timeline determines urgency and session prioritization. Weights reflect relative importance — Signal Strength and Info Asymmetry get highest weights because they most directly predict alpha.

**Alternatives rejected**:
- Unweighted scoring — treats all dimensions equally, but Signal Strength matters more than Liquidity
- Binary pass/fail system — loses nuance; a 35-score candidate should get more attention than a 31

---

## D-002: Daily Reporting Cadence (2026-04-09)

**Decision**: Daily signal reports and monitoring, not weekly.

**Why**: Many signals are time-sensitive — EDGAR filings, congressional trade disclosures, contract awards. A weekly cadence would miss opportunities where edge decay is measured in days. Daily scanning also catches convergences earlier.

**Alternatives rejected**:
- Weekly cadence — too slow for time-sensitive signals; edge decays before next scan
- Real-time/continuous — not feasible in Cowork session model; daily is the practical maximum

---

## D-001: 5 Strategies Selected from 19-Idea Longlist (2026-04-09)

**Decision**: Lock 5 strategies: EDGAR, ESMA Shorts, Congressional Trading, Contract Awards, FDA PDUFA.

**Why**: Selected via three hard filters: (1) Claude can execute autonomously — free APIs, no auth walls, no CAPTCHA; (2) data is legally public — no proprietary licenses; (3) edge is structural, not temporarily unnoticed.

**Alternatives rejected** (with specific reasons):
- Director Entity Forensics — no API, requires manual Companies House searches
- CENDOJ (Spanish courts) — hostile to automation, no API
- Jet Tracking (ADS-B) — GDPR concerns, data quality issues
- Hedging Roll-Off Calendars — too slow, requires options data
- BOE/Gazette Scanning — high false positive rate
- Google Trends anomalies — noise-to-signal ratio too high for systematic use
- Full longlist comparison: `outputs/investment_ideas_comparison.xlsx`

---

## D-000: $300M Minimum Market Cap (2026-04-09)

**Decision**: All strategies filter for companies with market cap ≥ $300M.

**Why**: Below $300M, liquidity risk dominates any signal quality. Position entry/exit becomes the primary risk, not the thesis. $300M ensures adequate daily volume for satellite positions (2-5% portfolio) in most cases.

**Alternatives rejected**:
- $500M floor — misses interesting mid-caps, especially in European markets
- $100M floor — too many illiquid names, position sizing becomes impractical
- No floor — would flood the system with untradeable micro-caps

---

## D-032 — MNKD DEMOTED from prelim 35.0 to watchlist 26.75–27.75
**Date**: 2026-04-10 (Session 24)

**Decision**: Move MNKD from the preliminary candidate pool (prelim 35.0, above 28 threshold) to the watchlist band (26.75–27.75, below threshold). Do NOT write a full candidate file.

**Alternatives considered**:
1. Elevate at 35.0 and write candidate file. REJECTED — the prelim score was built on an unvalidated assumption about the selloff driver.
2. Park in research queue at 30-32. REJECTED — splits the difference without resolving the core regulatory risk.
3. Archive entirely. REJECTED — insider buying, Tyvaso decoupling, and analyst dispersion still provide some asymmetry worth monitoring.

**Rationale**: Mandatory 7-flag validation exposed that INHALE-1 Phase 3 MISSED its primary ITT non-inferiority endpoint (HbA1c between-group difference 0.435% exceeded the pre-specified 0.4% NI margin). Only the post-hoc mITT analysis — which excluded one non-adherent subject — met the threshold at 0.370%. FDA acceptance of post-hoc mITT analyses is uncertain and the pediatric efficacy bar has tightened in recent years. This is a fundamental regulatory risk that the Session 23 thesis ("approval delayed by Tyvaso DPI overhang, approval probability ~70%") did not account for. Secondary endpoints (safety, hypoglycemia, treatment satisfaction, weight gain) favor approval but cannot carry an sBLA where primary efficacy is disputed. Tyvaso-related revenue also confirmed at ~65% of 2025 total (Afrezza is a smaller lever than prelim assumed). CEO Castagna March 1 buy of 15,290 shares at $3.27 is modestly bullish but insufficient to offset. Revised dimensions: Signal Strength 3.5→3.0, Info Asymmetry 4.0→2.5, Risk/Reward 5.0→2.5, Edge Decay 3.5→3.0. Final range 26.75–27.75 including haircut for ITT-execution uncertainty. This is the **second consecutive adversarial self-correction** (Session 23 VRDN double-count; Session 24 MNKD prelim). The review process is working.

**Implications**:
- MNKD does NOT get a candidate file.
- Watchlist monitoring only. Triggers: AdCom announcement (would raise CRL probability further), insider buying continuation <$2.50 (revisit), price break $2.20 (invalidation / archive) or $2.80 (re-rating / revisit).
- Next deep check T-7 (~May 22) pre-PDUFA.
- Process lesson: Flag 2 (clinical data quality) MUST be verified before any preliminary score above 28 is assigned for an FDA catalyst candidate.

**Source**: `working/session24_mnkd_deep_dive.md`

---

## D-033 — TVTX score held at provisional 29.75 despite REPL April 8 -24.5% crash
**Date**: 2026-04-10 (Session 24)

**Decision**: Hold TVTX provisional score at 29.75 at the $31.44 entry level. Do NOT re-score downward in response to REPL's sector sell-off.

**Alternatives considered**:
1. Lower TVTX to ~27 on REPL read-across (sector risk). REJECTED — conflates two structurally different catalysts.
2. Raise TVTX to 30.5 on TVTX/REPL divergence as bullish signal. REJECTED — divergence is positive but modest; does not warrant a score increase that has already been credited.

**Rationale**: TVTX and REPL are structurally different catalysts. TVTX = label expansion for already-approved Filspari (FSGS sNDA) with NEJM-published Phase 3 DUPLEX data, AdCom bypassed (agency confidence signal), and January 2026 review extension for benefit-recharacterization only (not safety/CMC). REPL = single-arm oncolytic virus (RP1) with a known July 2025 CRL history and CBER-vs-Pazdur precedent. Institutional flows April 7-9 correctly distinguished the two — TVTX held tight $31.44-$31.83 four-session coil on normal volume while REPL crashed 25%. This is institutional validation of thesis independence. REPL outcome is therefore a sentiment overlay (expect ±2-5% Monday open sympathy), not a thesis driver.

**Implications**:
- TVTX remains the active candidate at 29.75 provisional.
- Hold-existing only. DO NOT chase fresh entry at $31.44 (asymmetry compressed from $27-28 initial entry).
- Monday April 13 PDUFA remains the binary decision point.
- Next session must still verify REPL outcome (via direct EDGAR 8-K pull, not web snippets) to confirm divergence thesis post-resolution.

**Source**: `working/session24_tvtx_t1_presweep.md`

---

## D-034 — VRDN score held at 31.5 (do NOT re-score post-Amgen)
**Date**: 2026-04-10 (Session 24)

**Decision**: VRDN score remains at 31.5. Do NOT demote for the Apr 6 Amgen SC Tepezza Phase 3 77% PRR event.

**Alternatives considered**:
1. Demote VRDN to ~23-25 on Amgen competitive threat. REJECTED — this would double-count the Amgen event, which was already incorporated into the Session 23 score via Info Asymmetry 3 / Risk-Reward 4 downgrade.
2. Raise VRDN on stabilization pattern. REJECTED — stabilization is mild (+10.6% off the low on declining volume) and does not change the fundamental PDUFA event-reaction thesis.

**Rationale**: The Session 23 VRDN self-correction already demonstrated this error mode — the initial S23 draft tried to demote VRDN for Amgen, then caught that the 31.5 score was already post-Amgen. Same logic applies this session. The current stabilization pattern (Apr 9 close $15.38, +10.6% above Apr 6 low $13.90, volume declining 14.5M → 6.6M → 4.6M → 2.7M) is consistent with a completed re-rating after the initial one-day shock. Score holds. The thesis is PDUFA event-reaction on a dislocated stock, not long-term NPV — which means the Amgen event matters for the magnitude of the reaction but does not invalidate the binary.

**Implications**:
- VRDN remains active candidate at 31.5.
- Next deep check T-30 (late May).
- Watch for any VRDN 8-K, REVEAL-2 Q2 readout, or Amgen SC Tepezza formal BLA filing.

**Source**: `working/session24_vrdn_monitor.md`

---

## D-035 — HROW added to watchlist at 22.5
**Date**: 2026-04-10 (Session 24)

**Decision**: Add HROW to watchlist band at score 22.5. Do NOT write a candidate file.

**Alternatives considered**:
1. Archive / ignore. REJECTED — the signal cluster is meaningful (new institutional short + crowded book + rising price).
2. Elevate to research queue or candidate. REJECTED — no catalyst narrative to anchor the thesis.

**Rationale**: HROW has a real signal cluster: Walleye Capital LLC new 0.52% short disclosure to BaFin (2026-04-09), 21.1% short interest of float (crowded), and a +12.0% price move over 10 trading days into the new short. Mkt cap $1.39B is well above the $215M floor. Two plausible readings: (a) sophisticated short front-running a specific catalyst (earnings miss, regulatory action, litigation, competitive erosion), or (b) contrarian squeeze precursor (high SI + rising price + low volume float). Without a catalyst narrative to distinguish, cannot elevate. HROW is exactly the kind of signal that benefits from convergence detection — if EDGAR (going concern, activist 13D/G) or Congressional trading also flags HROW in the 14-day window, the convergence engine should surface it.

**Implications**:
- Watchlist entry only. Monitor for Q1 2026 earnings (expected late April / early May) as the most likely near-term catalyst.
- If earnings miss and stock gaps >10%, revisit thesis on the bearish side.
- If squeeze materializes (volume spike + price break >$40), revisit on the bullish side.
- Convergence detection is the key escalation path — watch for second-strategy confirmation.

**Source**: `working/session24_hrow_triage.md`

---

## D-029-B — PIPE/SPA anti-takeover "waiver" boilerplate is NOT a distress signal
**Date**: 2026-04-11 (Session 32); formally logged 2026-04-12 (Session 33)

**Decision**: Extend D-029-A (routine filings boilerplate) with a specific corollary: any "waiver" keyword hit inside exhibits labelled EX-10.x where the exhibit is a PIPE Securities Purchase Agreement (SPA), Registration Rights Agreement, Indenture supplement, or Finance Agreement Amendment is presumed boilerplate unless corroborated by a separate independent signal.

**Alternatives considered**:
1. Leave the rule at D-029-A generality. REJECTED — the S33 ASPI case (EX-10.44 Finance Agreement amendment from 2020, "No Waiver" clause) demonstrated that the triage time cost is nontrivial; a more specific corollary speeds the decision.
2. Hard-archive all EX-10.x waiver hits without review. REJECTED — would miss legitimate debt covenant waiver events where a company is in actual distress.
3. The chosen corollary: soft-presumption of boilerplate, requires one corroborating signal (insider selling cluster, 10% stock drop in 30 days, going-concern language elsewhere, activist 13D, analyst downgrade) before elevation.

**Rationale**: EX-10.x exhibits in 10-Ks, 10-Qs, and 8-Ks are routinely amended finance agreements, PIPE SPAs, or registration rights agreements. The SEC's disclosure convention requires including anti-waiver and no-waiver clauses ("The execution, delivery, and effectiveness of this Amendment shall be limited precisely as written and... shall not be deemed to be a consent to any waiver") — these clauses trigger the keyword scanner but contain zero information about actual covenant stress, default, or waiver consent. S33 confirmed this pattern across ASPI (retrospective 2020 DFC TETRA4 amendment), EXPE (mega-cap indenture supplement), and prior sessions' observations.

**Implications**:
- Triage path: on any "waiver" EDGAR hit, first classify by filing type and exhibit type. If EX-10.x + finance/PIPE/indenture agreement → apply D-029-B presumption → look for one corroborating signal before escalation.
- Scanner improvement (future): add EX-10.x filing-type exclusion to the activist_keyword scanner, OR add a post-filter that flags these hits as "low-confidence" rather than rejecting outright.
- Running tally: confirmed D-029-A/B false positives in S32 and S33 now at ~10 distinct cases; the pattern is robust enough that scanner-level filtering is justified.

**Source**: `working/session32_edgar_fda_triage.md`, `working/session33_edgar_fda_triage.md`

---

## D-036 — Pre-PDUFA price-action as a T-1 confidence overlay (inverse rule)
**Date**: 2026-04-10 (Session 31); formally logged 2026-04-12 (Session 33)

**Decision**: For any active FDA PDUFA candidate, use the T-1 to T-5 price-action pattern as a binary confidence overlay on the existing score. The rule is the **inverse** of the naive read: a pre-PDUFA crash on high volume is bearish, but the absence of a crash is materially bullish — and specifically, an UP day in a known-weak biotech tape is the strongest pre-decision bullish signal available.

**Alternatives considered**:
1. Use only fundamental/regulatory inputs; ignore price action entirely. REJECTED — price action contains real information about institutional positioning and leakage.
2. Use price action directly (crash = CRL, rally = approval). REJECTED — too noisy on the bullish side; rallies often reflect speculative retail front-running, not institutional knowledge.
3. The chosen asymmetric inverse rule: bearish signal is high-confidence (crashes usually correspond to real leaked information); bullish signal is the *absence* of a crash during sector weakness, not a rally.

**Rationale**: The REPL April 8 2026 test case provided the empirical anchor: REPL crashed -24.6% on 2.0× volume April 8, confirmed as CRL April 9 — textbook pre-PDUFA bearish signal. On the same day, TVTX closed UP while the broader biotech tape was weak. This is the inverse signal: TVTX's failure to participate in the REPL-triggered biotech sell-off, despite TVTX being in the exact same FDA-binary window, is a bullish divergence. The institutional logic: shorts who would front-run a TVTX CRL would have piled in during the REPL panic to get size on at elevated IV. That they did not do so materially updates the prior on TVTX approval odds. Framework rule: apply this overlay **only** during broad biotech weakness; in a calm tape, the inverse signal is not operative because shorts have no pretense for positioning.

**Implications**:
- On any PDUFA candidate, define a T-5 to T-1 window for inverse-rule monitoring.
- If the candidate closes UP on a day when the biotech/therapeutic-area index is DOWN ≥2%, count this as +0.5 to +1.0 confidence weighting toward the approval thesis (NOT a score change — the underlying dimensions are unchanged, but entry/position sizing can lean further).
- If the candidate closes DOWN ≥5% on ≥2× volume during the T-5 to T-1 window without a clear sympathy catalyst, treat as a kill-adjacent warning: downgrade score 3-5 points, demote from candidate to watchlist pending immediate investigation.
- Document each invocation and post-outcome calibration. The rule needs N>5 observations before hardening.

**Source**: `working/session31_tvtx_kill_sweep.md`, `working/session32_tvtx_final_sweep.md`, `working/session33_tvtx_final_sweep.md`

---

## D-037 — Vanguard administrative realignment zero 13G/A is NOT a position exit
**Date**: 2026-04-12 (Session 33)

**Decision**: When a Vanguard Group (or Vanguard sub-entity) SC 13G/A filing shows 0 shares owned, verify whether the filing is part of the firm's January 12, 2026 internal organizational restructuring (pursuant to SEC Release No. 34-39538) by reading the exhibit commentary. If so, classify as an administrative artifact, NOT a position exit. Do NOT generate a bearish signal on the issuer from such filings.

**Alternatives considered**:
1. Treat all zero 13G/A filings as bearish position-exit signals. REJECTED — the Vanguard restructuring is affecting thousands of issuer filings system-wide and would produce a flood of false bearish signals across our entire coverage universe.
2. Filter all Vanguard 13G/A filings from the scanner permanently. REJECTED — legitimate Vanguard position changes still contain signal; a blanket exclusion would miss real exits.
3. The chosen approach: content-inspect zero-share Vanguard 13G/A exhibits for the SEC Release 34-39538 language, and tag matches as administrative.

**Rationale**: The VERA deep-dive surfaced a March 27 2026 Vanguard SC 13G/A showing 0 shares. Initial reading treated this as a material Vanguard exit, which conflicted with the institutional-bullish narrative (Point72 new position April 1, Deep Track 5.5% stake March 11). Exhibit content inspection revealed the note: "On January 12, 2026, The Vanguard Group, Inc. went through an internal realignment... in accordance with SEC Release No. 34-39538." This is a system-wide administrative event affecting ALL Vanguard filings, not a VERA-specific position decision. Without this rule, every active candidate with any Vanguard exposure risks producing a false bearish convergence signal in the coming weeks as Vanguard completes its realignment filings.

**Implications**:
- Scanner enhancement (future): add Vanguard CIK filter + exhibit-body inspection for "Release No. 34-39538" or "internal realignment" language; flag as administrative.
- For any existing candidate with a zero 13G/A Vanguard filing in 2026, do NOT reduce institutional confidence scoring until the exhibit is content-inspected.
- Cross-reference: Vanguard Group Inc CIK is 0000102909 and related sub-entities.
- Running tally: 1 confirmed instance (VERA March 27). Expect more — update this decision with counter count as each is observed.

**Source**: `candidates/VERA_IGAN_PDUFA.md`, EDGAR filing https://www.sec.gov/Archives/edgar/data/1831828/000010290926002499/primary_doc.xml
