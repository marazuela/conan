# Open Questions

*Items requiring user input, unresolved technical issues, or decisions deferred for future sessions.*

---

## Q-016: Add terminal-marker validation to Phase 2a compile sweep (2026-04-14, maint-22:59)

**Status**: OPEN — proposed enhancement, awaiting user approval. Low risk, high reliability value.
**Trigger**: 3rd file-truncation incident on `convergence_engine.py` this session included a NEW silent variant — file cut mid-statement at `report = gener` — that passed both `py_compile` and `ast.parse`. Current Phase 2a uses py_compile as sole validation gate, which cannot detect this class of corruption.
**Proposed fix**: Extend Phase 2a in the maintenance prompt to include a "terminal-marker check" after compile. For each tool file, assert that the last non-blank line matches an allowlist of expected terminal forms (e.g., `main()`, `sys.exit(main())`, `print(...)` for utility scripts). A dict per-file of expected-terminal-marker substrings is the simplest implementation. Any mismatch → flag for investigation via the Read tool.
**Indicative allowlist** (from this session's clean tail audit):
- congressional_trading, contract_monitor, convergence_engine, edgar_filing_monitor, esma_short_scanner, fda_pdufa_pipeline, openfigi_resolver, pipeline_runner: must end with `main()`
- run_post_scan, run_scanner: must end with `sys.exit(main())`
- companies_house_monitor, mcap_cache, google_trends_scanner, uk_gazette_insolvency_scanner: library/utility — must end in `print(...)` at module scope (last non-blank is an output statement)
**Priority**: Medium-high. This is explicitly a latent-bug mitigation for Warning #1 (file-truncation bug) which has now fired 3 times and keeps morphing.
**Scope note**: Not implemented this maintenance session — the maintenance mandate limits scope to fixes, not architectural changes. Flagging here for user approval before adding to the maintenance prompt.

---

## Q-001: Approval to Begin Building (RESOLVED)

**Status**: ✅ APPROVED — Pedro gave the green light on 2026-04-09
**Action**: Begin tool development immediately. Follow priority queue in INSTRUCTIONS.md: OpenFIGI module → EDGAR refactor → Congressional client → ESMA tool → Contract monitor → FDA pipeline → Convergence engine → Integration + first scan.

---

## Q-004: FDA PDUFA Pipeline — Early Approval Cross-Check Needed

**Status**: ✅ RESOLVED — `run_approval_crosscheck()` implemented in fda_pdufa_pipeline.py v2.0. Runs automatically each scan, checks openFDA drugsfda database for approval status. CORT already in DISQUALIFIED_TICKERS. S45 verified working (1 approved entry, 37 active). Additionally, ORCA and BEREN tagged non-tradeable in S45.
**Context**: Session 40 discovered that CORT (relacorilant) was approved on March 25, 2026, ~3.5 months ahead of its July 11, 2026 PDUFA date. Sessions 38-39 had been scoring CORT as an active PDUFA candidate without realizing the event had already resolved. This is a data quality gap in the FDA PDUFA pipeline.
**Proposed fix**: Add an FDA approvals cross-check to `tools/fda_pdufa_pipeline.py`. Before emitting a PDUFA signal, query the FDA Drug Approvals database (Drugs@FDA or openFDA) to check if the drug/company has already received approval. If approved, mark the watchlist entry as "approved" and suppress the signal.
**Priority**: Medium — this is a data quality issue but not a safety issue. The web research layer caught it this time. Implementing the check would catch it earlier and automatically.
**See**: D-046 in DECISIONS.md, `working/session40_cort_approval_discovery.md`

---

## Q-002: CNMV (Spain) Access Strategy

**Status**: Deferred to Phase 3 of ESMA rollout
**Context**: CNMV returns 403 Forbidden from the Python sandbox. This is Pedro's home market, so it has strategic value.
**Options**:
1. Use Claude in Chrome (browser automation) to access CNMV — adds complexity but keeps it automated
2. Pedro manually checks CNMV periodically and adds data to the system
3. Defer indefinitely — FCA and Bundesanzeiger cover UK and Germany, which may be sufficient
**Needs**: Pedro's preference on priority and approach

---

## Q-003: PDUFA Calendar Source

**Status**: ✅ RESOLVED — Watchlist populated (Session 13)
**Context**: `signals/pdufa_watchlist.json` now contains 9 verified PDUFA entries (Apr 13 – Jul 7 2026): TVTX, MNKD, ACHV, ARQT, LNTH, IONS, AZN, PFE, VERA. All verified via web research. The FDA scanner now has data to work with.
**Update (Session 15)**: Added VRDN (Viridian Therapeutics, veligrotug, TED, Jun 30). Watchlist now 10 entries.
**Ongoing maintenance**: Watchlist should be refreshed every 2-4 weeks as new PDUFA dates are announced. Watch for CRL/approval outcomes on approaching dates (TVTX Apr 13 is first).

---

## Q-004: Scheduled Session Configuration (RESOLVED)

**Status**: ✅ RESOLVED — Session 19 (April 10, 2026)
**Resolution**: Scheduled task `investment-tool-project` configured and enabled via `create_scheduled_task` tool. Runs every 3 hours, every day (including weekends) with full autonomous prompt (orient → scan → score → monitor → regenerate docx → shutdown). Concurrency lock via `SESSION_LOCK.md` prevents overlap. A companion maintenance task (`investment-tool-maintenance`, cron `50 */3 * * *`) was added in S46 — see D-047.
**Schedule**: Every 3 hours, every day (cron: `0 */3 * * *`), local timezone. Updated from weekdays-only (`0 9 * * 1-5`) to every-3h-every-day in S46 per user request.
**Note**: First run after enabling should be triggered manually ("Run now") to pre-approve any tool permissions the task needs.

---

## Q-005: PROGRESS_LOG.md Growth Management

**Status**: Future consideration
**Context**: PROGRESS_LOG.md is append-only and will grow indefinitely. After 20+ sessions, it will become large. Current mitigation: new sessions read SESSION_STATE.md (not PROGRESS_LOG) for current state. PROGRESS_LOG is only consulted for historical context when needed.
**Options when it becomes too large**:
1. Archive old entries (move sessions 1-N to `archive/progress_log_sessions_1-N.md`, keep recent entries in main file)
2. Summarize old entries into a single "history summary" paragraph
3. Keep as-is — new sessions rarely need to read it
**Decision**: Defer until it becomes a concrete problem (likely after 15-20 sessions).

---

## Q-006: Quiver Quantitative Now Requires Auth

**Status**: Resolved — switched to Capitol Trades (see D-013)
**Context**: Quiver Quantitative API was free during feasibility testing but now returns 401 (auth required). Congressional trading tool was built using Capitol Trades HTML scraping instead. No data quality loss — both derive from STOCK Act filings.
**Note**: If Quiver offers a free tier again in the future, consider switching back for the richer data (ExcessReturn metrics). Monitor periodically.

---

## Q-007: Workspace Sandbox Timeout / Lock Recovery

**Status**: Active — mitigated but not fully resolved
**Context**: The Cowork bash sandbox has a 45s timeout limit. When a scanner (especially EDGAR) makes slow API calls, the Python process can exceed this timeout. When it does, the sandbox enters a locked state ("process already running") that persists for 10+ minutes, blocking ALL bash commands. This happened in Sessions 13 and 14.
**Mitigations applied**:
- D-014: pipeline_runner.py v1.1 now runs each scanner in a subprocess with a 120s hard kill timeout
- Created `tools/run_scanner.py` — runs ONE scanner per bash call (each call gets its own 45s window)
- Created `tools/run_post_scan.py` — aggregation/convergence/report in a separate call
**Update (Session 15)**: EDGAR wall-clock budget reduced from 90→35s (D-018). Scanner now finishes or gracefully saves partial results before 45s bash kill. Sandbox lock not observed in Session 15. Risk substantially mitigated.
**Remaining risk**: If EFTS API has a single very slow response that blocks the main thread before the budget check, the scanner could still exceed 45s. Rare, and data is saved before kill, so impact is sandbox lock (3 min), not data loss.
**Status**: Mitigated. Monitor — if sandbox lock recurs, consider rotating keyword categories across sessions.

---

## Q-008: FCA Short Selling Regime Change (June 2026)

**Status**: Active — impacts ESMA short scanner
**Context**: The FCA is implementing a new UK short selling regime in June 2026 (Phase 1). Key change: **individual short position disclosures at 0.5% threshold will be REPLACED by aggregate anonymized positions (ANSP)**. This means our ESMA short scanner will lose the ability to identify specific short holders (D.E. Shaw, Marshall Wace, etc.) and see individual position sizes.
**Impact timeline**:
- **Now through May 2026**: Current disclosure regime still active. Scanner works as designed.
- **June 2026 (Phase 1)**: New regime begins. Individual disclosures replaced by aggregate anonymized data published T+2.
- **December 2026 (Phase 2)**: Full implementation.
**Impact on strategy**:
- Short crowding detection (counting number of independent short holders) will be degraded — we'll only see aggregate %
- Individual holder identification (which helps distinguish fundamental shorts from quant/technical shorts) will be lost
- Aggregate data may still be useful for detecting significant short interest changes
**Options**:
1. Adapt scanner to use aggregate ANSP data when available (still useful for crowded short detection)
2. Supplement with non-UK sources (Bundesanzeiger Germany, AMF France) that may maintain individual disclosures
3. Consider commercial short interest data providers
4. Accept reduced signal quality from this strategy
**Decision**: Defer until June 2026 when new data format is available. Current scanner continues to work until then.

---

## Q-009: EDGAR Activist Category — Proxy-Season Noise Filter

**Status**: Active (D-030) — deprioritized during proxy season
**Context**: EDGAR activist rotation is blind during April proxy season because DEF 14A / PRE 14A filings dominate the form mix and dilute signal.
**Proposed fix**: Add a form whitelist (8-K, 13D, 13D/A) + exclude DEF 14A and PRE 14A during March-May window. Re-enable after mid-May.
**Decision**: Defer until build session available. Governance category also deprioritized during proxy season for the same reason.

---

## Q-010: EDGAR Distress + M&A — SPAC/De-SPAC Noise Filter (D-031)

**Status**: Active — impacts both `distress` and `mna` EDGAR categories
**Context**: During April 2026, SPAC/de-SPAC merger wave is contaminating both the distress and M&A rotation categories. The keyword-based EFTS query legitimately matches "merger agreement", "tender offer", and "fairness opinion" in S-4/A, DEFM14A, 425, SC TO-C, and 424B* forms, but these are pre-existing deal disclosures, not new signal. Session 22 distress scan: 30 signals, 2 survived pre-filter, 0 were real. Session 23 M&A scan: 38 signals, 0 real candidates above $215M floor.
**Proposed fix** (both categories):
1. **Form whitelist** in `tools/edgar_filing_monitor.py:scan_keywords()`:
   ```python
   ALLOWED_FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"}
   EXCLUDED_FORMS_CONTAIN = ["S-4", "DEFM14", "425", "SC TO", "424B", "S-1"]
   ```
2. **SPAC issuer blacklist**: maintain a list of known SPAC issuers and filter them out entirely.
3. **Optional mcap floor raise** to $500M for these two categories specifically.
**Expected impact**: ~95% of signals dropped, ~2-5% survive to deep dive — most of which should actually be actionable.
**Decision**: Implement during next build session. Non-blocking for daily operations because manual bypass scans confirm 0 candidates anyway.

---

## Q-011: Candidate Kill-Condition Web-News Monitoring

**Status**: New (Session 23)
**Context**: Session 22 performed a kill-condition check on TVTX focused on price tape and did not re-scan VRDN's news flow for competitor events. VRDN had been hit by a major adverse competitive event (Amgen SC Tepezza Phase 3 77% PRR on April 6) that was correctly captured in Session 19 candidate file update but could have been missed if the monitoring cadence had been different. Session 23's initial rescore draft also nearly double-counted this event because the price-action-only check didn't surface the "already-scored" status.
**Proposed fix**: For every active candidate, add a weekly targeted news search for: (a) competitor regulatory events, (b) AdCom scheduling, (c) analyst PT revisions, (d) litigation/regulatory actions, (e) insider disposition trends. Build a simple `tools/candidate_news_monitor.py` that queries Google News / Seeking Alpha / Benzinga for each candidate ticker on a weekly cadence. Results appended to candidate file Web Research log.
**Alternative**: Incorporate the web-news layer into the existing monitoring session workflow rather than building a new tool — a lighter-weight checklist step.
**Decision**: Defer design decision until after TVTX PDUFA resolves. Non-blocking.

---

## Q-012: PROGRESS_LOG.md Truncation

**Status**: New (Session 23) — data integrity concern
**Context**: At Session 23 start, `PROGRESS_LOG.md` was found truncated at line 775 with the Session 20 entry ending mid-sentence. Sessions 20, 21, 22 entries are missing or incomplete. Primary session state is preserved in `working/session##_*.md` files and in the overwriting `SESSION_STATE.md`, so no decision-level information was lost, but the chronological audit trail has a gap.
**Possible causes**:
1. Previous session's shutdown protocol ran into a file-write truncation (unlikely — Edit tool appends cleanly)
2. A previous session overwrote the file with a partial version (possible)
3. File-system sync issue between the Windows host and the Linux sandbox mount
**Proposed fix**: 
1. Flag Sessions 20-22 as non-authoritative in this log (done in Session 23 entry)
2. Going forward, verify PROGRESS_LOG.md line count before and after every shutdown append
3. Consider rotating PROGRESS_LOG.md into `archive/` when it exceeds 500 lines (see also Q-005)
**Decision**: Mitigation applied (flag added in Session 23 entry). Full root-cause analysis deferred.oxy-Season Noise Filter

**Status**: Open — tool improvement deferred, workaround in place (D-030)
**Context**: In Session 21 (2026-04-10) the `activist` keyword rotation category produced 17 signals, all of which were noise. The root cause is that during March–May proxy season, DEF 14A, DEFA14A, and ARS filings dominate EDGAR full-text search, and they all contain CIC-severance and golden-parachute boilerplate that triggers keywords like "change in control" and "strategic alternatives." The governance category (Session 19/20) has the same issue.
**Proposed fix**: Add a proxy-aware form whitelist to the activist keyword scan. Only scan forms in {SC 13D, SC 13D/A, SC 14D9, PRER14A, DFAN14A, 8-K with Item 8.01/1.01 only}. Add a mcap floor (≥$500M) to the activist category specifically. Skip DEF 14A / ARS / DEFA14A from this category entirely.
**Interim workaround**: Rotate to distress/M&A categories during March–May. Re-enable activist rotation after proxy season ends (~mid-May).
**Owner**: Next implementation pass on `edgar_filing_monitor.py`. Not urgent — workaround preserves signal quality.

---

## Q-010: EDGAR Distress Category — SPAC/Pre-IPO Boilerplate Filter

**Status**: Open — tool improvement deferred (D-031), workaround is next-session rotation to `m_and_a`
**Context**: Session 22 (2026-04-10) ran the distress rotation. 30 raw signals → 28 dropped by pre-filter → 2 survived (APAD, NUCL) → **both rejected as noise** on inspection. APAD is a SPAC with an active S-4/A proxy stream for an Enhanced Ltd. merger; "going concern" is required SPAC-deadline boilerplate. NUCL is a pre-revenue uranium/nuclear company in its S-1/A registration flow; "going concern" is required pre-revenue S-1 risk factor. Both are 100% form-driven false positives.
**Root cause**: Distress keywords ("going concern", "substantial doubt", "ability to continue") are **required disclosures** under GAAP/SEC rules in S-1 and S-4 risk factors for every blank-check or pre-revenue issuer. The April de-SPAC and IPO wave produces dozens of these per week.
**Proposed fix** (tool change to `edgar_filing_monitor.py`, distress category only):
1. **Form whitelist**: accept only `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `8-K` (prefer Items 2.04, 3.01, 4.02, 5.02).
2. **Form blacklist**: exclude S-1, S-1/A, S-4, S-4/A, 425, DRS, DRS/A, N-CSR, SB-2, F-1, F-4.
3. **Issuer blacklist**: exclude tickers whose OpenFIGI name matches `"Acquisition Corp"`, `"Acquisition Company"`, `"Blank Check"`, `"Capital Corp"` + triple-ticker warrant/unit patterns (NUCLW, APADR, APADU).
4. **Optional mcap floor raise**: distress is higher-value at mid-caps ($500M-$5B) where analyst coverage creates asymmetry; consider lifting floor from $215M → $500M for this category.
**Interim workaround**: Next session, rotate EDGAR to `m_and_a` (index 3) instead of distress. Both activist (D-030) and distress (D-031) are now flagged for post-proxy-season re-enable.
**Owner**: Next implementation pass on `edgar_filing_monitor.py`. Coordinate with Q-009 fix — both are form-whitelist changes to category configurations.

---

*Last updated: 2026-04-10 (Session 22)*

---

## Q-013: D-036 Pre-PDUFA Price-Action Weighting Rule (DRAFT, observation-tracking)

**Status**: Open — drafted S31, requires 2-3 more observations before formalization
**Context**: REPL's Apr 8 2026 pre-announcement -24.6% single-day crash on 2.0× volume, with no discrete news trigger, was a strong leading indicator for the Apr 10 CRL outcome. This was validated post-hoc. Draft rule in `working/session31_d036_draft.md`.

**Core rule (draft)**: Within ≤5 trading days of a hard catalyst (PDUFA, AdCom, topline readout, FDA meeting outcome), a single-day price move ≥15% on volume ≥1.5× 20-day average without a discrete adverse/favorable news trigger is treated as a high-conviction directional leading signal about the eventual outcome. Immediate score adjustment (≥5 points) and potential archive/upgrade depending on direction.

**Evidence base**:
- **Confirming case**: REPL CRL Apr 10 2026 (pre-announcement -24.6% on 2.0× vol T-2 = CRL)
- **Weaker-form confirmation (long)**: AXSM Apr 9 breakout +3.3% on 1.85× vol at T-14 (outside strict ≤5-day window; day-2 test pending Monday)
- **Weaker-form confirmation (long)**: VERA Apr 10 +10.04% on 2.23× vol at T-62 (well outside window + partly news-driven by Wolfe upgrade/$200M investment)

**Boundary conditions considered**:
- Window: ≤5 trading days (stricter than weak form at 6-10d)
- Threshold: 15% single-day (10% too noisy in biotech)
- Volume: ≥1.5× 20-day average (REPL was 2.0×)
- News disambiguation: "Is there a named source of material information published within 2 hours of the price move?"
- Market cap floor: ≥$500M (smaller caps are too noisy)
- Reversal handling: Use close-to-close, not intraday

**Open calibration questions**:
1. Weight magnitude — immediate -5 points adequate, or demote-to-watchlist regardless of starting score?
2. Does same-day intraday reversal cancel the signal?
3. Precise test for "discrete news trigger" (FDA press release = clear, Bloomberg rumor = ambiguous)
4. Volume threshold floor — raise to 2.0× based on REPL evidence?
5. Short-thesis inverse application — no observations yet

**Next observations**:
- TVTX Monday Apr 13 PDUFA gap-direction (next empirical data point)
- Any biotech name with hard catalyst in next 5 trading days and ≥10% single-day move

**Mandatory action for each observation**: Log in `working/d036_evidence_log.md` (to be created S32). Tabulate: ticker, catalyst, catalyst date, T-N, pre-catalyst move magnitude, volume ratio, news status, eventual outcome, whether signal would have been correctly directional.

**Owner**: Scheduled sessions S32-S35. Formalize in DECISIONS.md only after 2-3 additional observations confirm or refine the rule.

---

## Q-014: Ro Khanna Spouse Mega-Cap Tech FP Pattern (framework refinement candidate)

**Status**: Open — pattern confirmed across 2 consecutive sessions (S30, S31), one more observation → formalize as DECISIONS entry

**Context**: Congressional scanner has produced strength-4 signals for 2 consecutive sessions that are ALL Ro Khanna Spouse (or Child) small-dollar ($1k-$50k) trades in mega-cap tech (AMZN, CRM, GOOGL, AAPL, META, NVDA, AVGO, ORCL, IBM). The scanner flags these as "committee_aligned:Commerce" which is technically correct (Ro Khanna sits on House Energy & Commerce) but the Commerce committee does not have access to material non-public information about iPhone sales, AWS revenue, or ad spend.

**Pattern signature**:
- Owner: Spouse OR Child (not Member)
- Trade size: $1,001-$50,000 (small dollar relative to household)
- Company: Mega-cap ($100B+) consumer/cloud tech
- Scanner category: committee_aligned, strength=4

**Proposed rule**: Exclude or auto-downgrade (strength 4→2) congressional signals where:
- Owner ∈ {Child, Spouse}
- AND trade range ≤ $50,000
- AND company market cap > $100,000 million
- AND committee alignment = "Commerce" (not Intelligence, Armed Services, Banking, or direct oversight)

**Evidence base**: 2 sessions (S30: 13 such signals; S31: ~20 such signals including 2 strength-4 in AMZN, CRM). Same owner, same size range, same ticker universe, same committee alignment across both sessions.

**Next action**: One more observation in S32 → if pattern persists, draft as D-series decision and apply filter to `congressional_trading.py`.

**Owner**: Scheduled sessions S32-S33.

---

*Last updated: 2026-04-14 (Session 57)*

---

## Q-015: EDGAR "substantial doubt" false-positive pattern in 8-K/A + SPAC boilerplate

**Status**: Open — pattern newly confirmed in S57 (2026-04-14 22:09 UTC distress rotation)

**Context**: S57 EDGAR distress rotation flagged 4 strength-4 "substantial doubt" signals. Full source-document analysis revealed ALL 4 are false positives from 2 distinct patterns:

**Pattern A — 8-K/A acquiree financials** (SERV, WERN)
- Company files 8-K/A to disclose audited historicals of a recently-acquired subsidiary
- Auditor's standard going-concern paragraph is about the ACQUIREE, not the parent
- Example WERN: "substantial doubt about FirstEnterprises, Inc.'s ability to continue" — FirstEnterprises was just bought by Werner; Werner itself is a $1.9B healthy trucking company
- Example SERV: EX-99.1 is "AUDITED CONSOLIDATED FINANCIAL STATEMENTS OF DILIGENT ROBOTICS, INC." — acquired by Serve

**Pattern B — SPAC boilerplate** (HYAC, RMIX)
- Standard SPAC auditor language: "if the Parent is unable to raise additional funds... complete a business combination by [date], then the Company will cease all operations... raises substantial doubt about the Company's ability to continue as a going concern"
- This language appears in EVERY SPAC's annual audited financials. It is not a distress signal.
- HYAC = Haymaker Acquisition Corp 4 (SPAC, pre-deSPAC). RMIX = Suncrete (appears to be a deSPAC still using SPAC auditor language).

**Proposed scanner fix** (candidate for `edgar_filing_monitor.py` next iteration):

1. **File-description filter**: If filing is `8-K/A` AND file_description contains any of {"audited", "financial statements of", "pro forma", "target", "acquiree", "acquired"} → downgrade strength 4 → 2 OR suppress entirely.

2. **SPAC filter** (new CIK-lookup layer): Check company name/classification for SPAC indicators {"acquisition corp", "SPAC", "blank check"} → suppress "substantial doubt" signal (SIC code 6770 is SPAC).

3. **Contextual passage filter**: Grep ±100 chars around "substantial doubt" for {"parent", "raise additional funds", "business combination", "cease all operations", "liquidating"} — all SPAC-specific phrases. Presence → suppress.

**Impact**: S57 would have gone from 9 EDGAR signals to ~5 real signals, eliminating 4 noise items. Across sessions this is meaningful analyst time savings.

**Evidence base (S57)**:
- SERV 8-K/A — file description literally says "AUDITED CONSOLIDATED FINANCIAL STATEMENTS OF DILIGENT ROBOTICS, INC. FOR THE YEAR ENDED DECEMBER 31, 2025"
- WERN 8-K/A — EX-99.1 contains "substantial doubt about FirstEnterprises, Inc."
- HYAC 8-K EX-99.1 (SPAC) — contains standard SPAC boilerplate about business combination deadline
- RMIX 8-K EX-99.1 — same SPAC boilerplate, same file naming (tm2611641d1_*) as HYAC

**Related finding**: S57 also surfaced LCID "covenant breach" as a false positive. The keyword appeared in the Subscription Agreement (EX-10.1) boilerplate about Fundamental Change Repurchase rights, not a real covenant breach. Actual LCID news is a massive POSITIVE capital raise (PIF + Uber private placement on Apr 14) — a separate signal that our current scanner does not capture. Consider adding "private placement", "PIPE", "capital raise" as a POSITIVE-signal keyword track in a future enhancement.

**Next action**: Build a filter patch in a future session. Test on S57's false-positive cohort; verify it does not also suppress legitimate going-concern signals from operating companies (the scanner has correctly caught real distress cases in prior sessions).

**Owner**: Future scheduled session when context budget allows.

---

*Last updated: 2026-04-14 (Session 57)*
