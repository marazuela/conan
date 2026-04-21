# DECISIONS SEED — Litigation & Docket Signal System (Tool 3)

These are the founding architectural decisions. The new session copies them **verbatim** into `DECISIONS.md` on day one of the working project. Once there, they are settled and not re-litigated per PROJECT_TEMPLATE Part 3.9.

If, during build, new evidence invalidates one of these decisions, the new session appends a later-numbered decision overriding it (never edits the original).

---

## D-000 — Founding architecture: litigation-domain instantiation of PROJECT_TEMPLATE

Date: <instantiation date>
Context: Tool 3 is the third tool in the Tool 1 / Tool 2 / Tool 3 family built against PROJECT_TEMPLATE. The prior adaptation conversation (captured in the bootstrap folder) produced the answers PROJECT_TEMPLATE demands before any work begins.
Decision: Instantiate the full PROJECT_TEMPLATE architecture (two-folder split, ten relay files, four-task topology, Tool Validation Protocol, cold-start and shutdown protocols, overwrite-only lock semantics) for the litigation-and-docket-signal domain. All non-negotiables from PROJECT_TEMPLATE apply verbatim. Domain adaptations: (a) new 7th scoring dimension (Party-Resolution Confidence) replacing Catalyst Timeline; (b) 30-day convergence window (widened from 14); (c) cadences slower than Tool 1's 3-hourly default (per-channel cadences in D-005); (d) new two-stage entity-resolution protocol (D-003); (e) free-sources-only policy with flag-for-manual for PACER (D-008).
Alternatives considered:
  - Build a bespoke architecture unconnected to the template. Rejected: loses cross-tool convergence capability and the battle-tested session-continuity machinery.
  - Extend Tool 1 with litigation scanners. Rejected: violates structural independence (D-004 of Tool 1/2); litigation requires different entity resolution, different cadence, different rubric — forcing it into Tool 1 would corrupt Tool 1's shape.
Implications: All downstream work assumes PROJECT_TEMPLATE discipline. Scheduled tasks use SESSION_LOCK. SESSION_STATE is rewritten every session. No delete operations — archive-only.

---

## D-001 — Structural independence from Tools 1 and 2

Date: <instantiation date>
Context: Tools 1 and 2 both enforce "own folder, own lock, own candidates, own reports; cross-system signal merging happens only via a separate analyzer project, never through direct file coupling." Tool 3 must choose whether to follow.
Decision: Tool 3 is structurally independent from Tools 1 and 2. Separate working folder (`litigation_system/`), separate `SESSION_LOCK.md`, separate candidate files, separate reports. Cross-tool convergence (Tool 3 + Tool 1, Tool 3 + Tool 2) happens in a separate analyzer project that reads from all three tools' candidate folders but does not write back.
Alternatives considered:
  - Share a lock file across all three tools. Rejected: coupling their failure modes; a Tool 1 crash would block Tool 3.
  - Write into a shared candidates folder. Rejected: no way to attribute, no way to dedup cleanly across the three entity-resolution regimes.
Implications: Cross-tool convergence is always a downstream read, never an upstream write. Any future analyzer project is a fourth deliverable, not a modification to Tool 3.

---

## D-002 — v1 scope: six litigation channels, US-only, $300M market-cap floor

Date: <instantiation date>
Context: Litigation is a vast domain. Federal criminal, state courts, bankruptcy, administrative law, international antitrust — each could be a tool of its own. Must pick a tractable v1 scope.
Decision: v1 covers exactly six channels: PACER/RECAP federal civil; ITC Section 337; PTAB IPR; Delaware Chancery; SEC enforcement; DOJ/FTC antitrust. US-listed equities only, $300M market-cap floor. Other channels (federal criminal, state courts beyond Delaware, bankruptcy, administrative warning letters, international) are deferred to Phase 2+ and re-evaluated after v1 produces validated candidates.
Alternatives considered:
  - Broader v1 including bankruptcy and state courts. Rejected: schema complexity for bankruptcy and volume explosion for state courts would extend build by 4–6 weeks with unclear marginal signal.
  - Narrower v1 with just PACER + ITC + PTAB (the three highest-precision channels). Rejected: loses M&A signals (Chancery, DOJ/FTC) which are among the highest-conviction outcomes.
Implications: Scanner count is fixed at six for v1. Sub-goals 1 and 5 in OBJECTIVES reflect this. Phase 2 re-evaluation is an explicit milestone in LITIGATION_PHASING.

---

## D-003 — Two-stage entity resolution; party-resolution confidence as a first-class scoring dimension

Date: <instantiation date>
Context: Tools 1 and 2 resolve entities from `ticker + MIC → FIGI`. Court captions use legal-entity names, not tickers; this single fact breaks the Tool 1/2 resolution protocol. Two-stage resolution (party-name normalization → entity resolution) is required, and the confidence of resolution is a first-order determinant of signal usefulness.
Decision: Adopt the two-stage protocol specified in `LITIGATION_CONTEXT.md` (strip corporate-form suffixes, classify party type, then try internal cache → SEC EDGAR exact → SEC EDGAR fuzzy → Exhibit 21 subsidiary → OpenFIGI NAME → unresolved, in that order). Record `resolution_method` and `resolution_confidence` on every signal. Signals with confidence < 0.85 are triaged out at Stage 1. Replace Tool 1/2's "Catalyst Timeline" dimension with "Party-Resolution Confidence" (×1 weight). Convergence engine keys on `issuer_figi` only; never on party-name string.
Alternatives considered:
  - Single-stage resolution directly via OpenFIGI NAME lookup. Rejected: precision < 50% in pilot testing; many false positives would poison the candidate pipeline.
  - Do not score confidence; rely on triage gate alone. Rejected: borderline-confidence signals (0.80–0.85) still reach the pipeline and need to be scored down, not just gated in/out.
Implications: Resolution cache is a project-lifetime dataset that grows monotonically (D-009). Executive-lookup table is a derived dataset (D-010). Scoring rubric has one dimension (Party-Resolution Confidence) that Tool 1/2 analysts will not recognize.

---

## D-004 — Common signal JSON schema compatibility

Date: <instantiation date>
Context: Tool 1 and Tool 2 share a signal JSON schema. Cross-tool convergence requires Tool 3's schema be structurally compatible so a single analyzer can read all three.
Decision: Tool 3's outer signal schema matches Tool 1/2 verbatim. Litigation-specific fields (court, case_number, case_caption, party_role, party_raw_name, resolution_method, resolution_confidence) live inside `raw_data`, not as new top-level fields. Tool 3's `entity_id` is CIK (same as Tool 1). `signal_category` values are new but drawn from the same flat-string pattern.
Alternatives considered:
  - Add top-level litigation fields. Rejected: breaks Tool 1/2 compatibility; future analyzer would need schema-version-dispatch logic.
  - Entirely new schema. Rejected: convergence across tools becomes a translation problem.
Implications: Tool 3 convergence engine and any future cross-tool analyzer can use the Tool 1/2 schema-parsing code unmodified at the outer level.

---

## D-005 — Per-channel cadences; 30-day convergence window

Date: <instantiation date>
Context: Tool 1 runs scanners every 3 hours. Litigation events move more slowly — most dockets update daily at most, and many key events (PTAB, ITC) post weekly or monthly. A 3-hourly cadence would produce ≥ 95% redundant reads and waste scheduler budget.
Decision: Per-channel cadences — Federal Civil and SEC Enforcement every 6h; ITC, Delaware Chancery, DOJ/FTC every 12h; PTAB daily. Operational task runs every 6h and dispatches scanners according to per-channel cadence (tracked via per-channel timestamp files). Convergence window is 30 days (wider than Tool 1/2's 14 days) because litigation-signal chains on the same entity span weeks.
Alternatives considered:
  - Single 6h cadence for all channels. Rejected: wastes reads on slow channels.
  - Hourly for federal civil. Rejected: RECAP updates are not that frequent; 6h is adequate.
  - 14-day convergence window for compatibility. Rejected: empirically, same-entity multi-channel litigation chains span 3–6 weeks.
Implications: Operational task is 6-hourly with sub-dispatch. Maintenance is 50 min after operational per template Part 4. Performance report daily, deep-dives every 8h. All four tasks share a single `SESSION_LOCK.md`.

---

## D-006 — US-only v1; non-US deferred

Date: <instantiation date>
Context: Tool 2 covers nine non-US exchanges. Litigation tools for those jurisdictions exist (UK Commercial Court, EU General Court, German Federal Court, etc.) but require different entity-resolution (no single-country analog to CIK) and different procedural schemas.
Decision: US-only for v1. Defer non-US litigation (UK Commercial Court, EU General Court, Canada Federal Court, etc.) to Phase 8+, re-evaluated after v1 produces validated candidates and the party-resolution cache has matured.
Alternatives considered:
  - Include UK Commercial Court in v1 (highest-value non-US litigation venue for public companies). Rejected: UK party resolution is its own problem (Companies House lookup, parent-company identification) and would extend build by 3–4 weeks.
Implications: Tool 3 candidates are a US-market subset only. Non-US litigation signals against US-listed ADRs (and cross-border antitrust) are partially captured by SEC Enforcement and DOJ/FTC channels but not by international scanners.

---

## D-007 — Federal criminal, state courts (ex-Delaware), bankruptcy out of v1

Date: <instantiation date>
Context: These three categories each have potential equity-signal value but different-shape data.
Decision:
  - Federal criminal: EXCLUDED from v1. Market-relevant federal criminal actions against public companies are typically announced by DOJ press release and captured by the DOJ/FTC Antitrust channel's press-release monitor. Adding federal-criminal PACER scanning would produce mostly noise.
  - State courts (ex-Delaware): EXCLUDED from v1. Volume is prohibitive (50 state systems, no unified index), and the vast majority of equity-material state litigation either consolidates into federal multi-district litigation (picked up by PACER) or is a Delaware Chancery matter (picked up by Delaware channel).
  - Bankruptcy courts: EXCLUDED from v1; DEFERRED TO PHASE 2. Bankruptcy has a specialized docket schema (adversary proceedings, 363 sales, reorganization plans) that requires dedicated schema work. High signal but specialized build.
Alternatives considered:
  - Include bankruptcy adversary proceedings only. Rejected: even that subset requires bespoke parsing and would extend v1 build.
Implications: Tool 3 v1 covers ~80% of equity-relevant docket signal volume with ~30% of the build complexity of an all-inclusive version.

---

## D-008 — Free-sources-only policy; PACER flag-for-manual pattern

Date: <instantiation date>
Context: PROJECT_TEMPLATE mandates "free APIs / free disclosure portals only." PACER costs $0.10/page and is the source-of-record for federal court documents. RECAP (the free Free Law Project mirror) covers a meaningful but incomplete subset.
Decision: Tool 3 v1 never autonomously spends PACER credits. All scanners use RECAP for document bodies; when RECAP lacks a document, the scanner records the docket entry (index metadata is always free via CourtListener) and emits a signal with `raw_data.document_status = "in_pacer_only"` and `raw_data.pacer_cost_estimate_cents = <page_count × 10>`. Deep-dive analysis flags these in `working/pacer_pulls_requested.md` for user-directed manual retrieval. This preserves the "free only" mandate for the autonomous system while allowing the user to retrieve specific documents out-of-band at their discretion.
Alternatives considered:
  - Allow a monthly PACER budget (e.g., $50/mo) for autonomous pulls. Rejected: violates mandate; opens door to unbounded cost growth; adds billing-reconciliation complexity.
  - Only scan RECAP; do not record missing-document metadata. Rejected: loses the information that a document exists, which is itself a signal.
Implications: v1 scan coverage for document bodies is RECAP-only. Index metadata coverage is full (via CourtListener). Some deep-dive analyses will have to proceed from docket-entry descriptions alone, without the filed document body. Phase 2 may revisit.

---

## D-009 — Internal party-to-issuer resolution cache: monotonic, session-lifetime

Date: <instantiation date>
Context: Every resolved party name is a lookup result that should not need to be redone. Recomputing resolutions every session wastes API calls and budget.
Decision: Maintain `baselines/party_resolution_cache.json` (or SQLite) as a monotonically-growing mapping from normalized party string → resolution result (method, confidence, issuer_figi, CIK, ticker, last-verified timestamp). Every successful resolution writes an entry. Cache entries have a `last_verified` timestamp; if > 180 days old, re-verify on next encounter (subsidiary relationships can change via M&A). Never delete entries — mark stale and re-verify. Cache is a checkpointed artifact in archive cycles.
Alternatives considered:
  - Recompute every session. Rejected: wastes API budget and slows the pipeline.
  - No stale-check; cache forever. Rejected: subsidiaries get spun off; parent-child mappings decay.
Implications: Cache is a project-lifetime asset. Performance improves over time. Cross-session continuity requires the cache file be present and valid.

---

## D-010 — Executive-lookup table for SEC enforcement executive-respondent signals

Date: <instantiation date>
Context: SEC Enforcement channel frequently names individuals (not just entities). To classify "is this executive employed by a public company in the universe," need a lookup table.
Decision: Maintain `baselines/executive_lookup.json` — a mapping of normalized executive name → list of (issuer_figi, role, as-of-date). Built from SEC DEF 14A (proxy statement) filings for in-universe companies; updated quarterly as part of baseline maintenance. Resolution on executive name is case-insensitive, ignores middle initials, with fuzzy-match fallback (Levenshtein ≤ 2 on full name).
Alternatives considered:
  - Resolve executive names via web search at scan time. Rejected: unreliable, slow, and produces inconsistent results.
  - Skip executive-respondent signals entirely. Rejected: executive-only SEC actions are a valuable under-followed sub-signal.
Implications: Quarterly executive-lookup refresh is a maintenance-task responsibility. Proxy-statement parsing is a new dependency. Executive-respondent signals have their own resolution-confidence scale (distinct from entity resolution).

---

## D-011 — Archive, never delete; overwrite-only lock semantics

Date: <instantiation date>
Context: PROJECT_TEMPLATE non-negotiables #1 and #7 mandate overwrite-only locks and never-delete / always-archive. No deviation.
Decision: Follow PROJECT_TEMPLATE non-negotiables verbatim. Archive path: `archive/YYYY-MM-DD_<reason>/`. Lock file uses overwrite-only semantics; even on abnormal termination, never `rm`. 4-hour stale-lock window per template Part 3.6.
Alternatives considered: None — template non-negotiable.
Implications: Deletion of superseded files requires manual user action outside the autonomous system. Accepted cost.

---

## D-012 — Scheduled-task naming and cron offsets

Date: <instantiation date>
Context: PROJECT_TEMPLATE Part 4 specifies cron-offset logic (operational at HH:00, maintenance at HH:50, ~10 minutes before next operational). Adapt to 6-hourly operational cadence.
Decision: Four scheduled tasks:
  1. `litigation-operational` — cron `0 */6 * * *`, write scope `litigation_system/`, SESSION_LOCK.
  2. `litigation-maintenance` — cron `50 */6 * * *`, write scope `litigation_system/` (audit-only), SESSION_LOCK.
  3. `litigation-performance-report` — cron `30 1 * * *` daily, write scope `reporting_layer/performance_reports/`, independent.
  4. `litigation-deep-dives` — cron `30 */8 * * *`, write scope `reporting_layer/litigation_briefs/`, independent.
Naming: `litigation-` prefix consistently; date suffix in the in-file session identifier.
Alternatives considered:
  - Separate scheduled task per channel. Rejected: would require six locks or six-way coordination; coordination cost exceeds benefit for sub-daily channels.
Implications: Four tasks coexist under one lock. Task registration is a Phase 0 milestone.
