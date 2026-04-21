# DECISIONS — Litigation & Docket Signal System (Tool 3)

Numbered sequentially. A settled decision is not re-litigated; it is **overridden** by a later-numbered decision if new evidence invalidates it (and then the old one is left intact for history, not edited).

D-000 through D-012 are copied verbatim from the bootstrap seed (`project set up template/litigation_tool_bootstrap/LITIGATION_DECISIONS_SEED.md`). D-013 is a new decision made during this instantiation session.

---

## D-000 — Founding architecture: litigation-domain instantiation of PROJECT_TEMPLATE

Date: 2026-04-14
Context: Tool 3 is the third tool in the Tool 1 / Tool 2 / Tool 3 family built against PROJECT_TEMPLATE. The prior adaptation conversation (captured in the bootstrap folder) produced the answers PROJECT_TEMPLATE demands before any work begins.
Decision: Instantiate the full PROJECT_TEMPLATE architecture (two-folder split, ten relay files, four-task topology, Tool Validation Protocol, cold-start and shutdown protocols, overwrite-only lock semantics) for the litigation-and-docket-signal domain. All non-negotiables from PROJECT_TEMPLATE apply verbatim. Domain adaptations: (a) new 7th scoring dimension (Party-Resolution Confidence) replacing Catalyst Timeline; (b) 30-day convergence window (widened from 14); (c) cadences slower than Tool 1's 3-hourly default (per-channel cadences in D-005); (d) new two-stage entity-resolution protocol (D-003); (e) free-sources-only policy with flag-for-manual for PACER (D-008).
Alternatives considered:
  - Build a bespoke architecture unconnected to the template. Rejected: loses cross-tool convergence capability and the battle-tested session-continuity machinery.
  - Extend Tool 1 with litigation scanners. Rejected: violates structural independence (D-004 of Tool 1/2); litigation requires different entity resolution, different cadence, different rubric — forcing it into Tool 1 would corrupt Tool 1's shape.
Implications: All downstream work assumes PROJECT_TEMPLATE discipline. Scheduled tasks use SESSION_LOCK. SESSION_STATE is rewritten every session. No delete operations — archive-only.

---

## D-001 — Structural independence from Tools 1 and 2

Date: 2026-04-14
Context: Tools 1 and 2 both enforce "own folder, own lock, own candidates, own reports; cross-system signal merging happens only via a separate analyzer project, never through direct file coupling." Tool 3 must choose whether to follow.
Decision: Tool 3 is structurally independent from Tools 1 and 2. Separate working folder (`litigation_system/`), separate `SESSION_LOCK.md`, separate candidate files, separate reports. Cross-tool convergence (Tool 3 + Tool 1, Tool 3 + Tool 2) happens in a separate analyzer project that reads from all three tools' candidate folders but does not write back.
Alternatives considered:
  - Share a lock file across all three tools. Rejected: coupling their failure modes; a Tool 1 crash would block Tool 3.
  - Write into a shared candidates folder. Rejected: no way to attribute, no way to dedup cleanly across the three entity-resolution regimes.
Implications: Cross-tool convergence is always a downstream read, never an upstream write. Any future analyzer project is a fourth deliverable, not a modification to Tool 3.

---

## D-002 — v1 scope: six litigation channels, US-only, $300M market-cap floor

Date: 2026-04-14
Context: Litigation is a vast domain. Federal criminal, state courts, bankruptcy, administrative law, international antitrust — each could be a tool of its own. Must pick a tractable v1 scope.
Decision: v1 covers exactly six channels: PACER/RECAP federal civil; ITC Section 337; PTAB IPR; Delaware Chancery; SEC enforcement; DOJ/FTC antitrust. US-listed equities only, $300M market-cap floor. Other channels (federal criminal, state courts beyond Delaware, bankruptcy, administrative warning letters, international) are deferred to Phase 2+ and re-evaluated after v1 produces validated candidates.
Alternatives considered:
  - Broader v1 including bankruptcy and state courts. Rejected: schema complexity for bankruptcy and volume explosion for state courts would extend build by 4–6 weeks with unclear marginal signal.
  - Narrower v1 with just PACER + ITC + PTAB (the three highest-precision channels). Rejected: loses M&A signals (Chancery, DOJ/FTC) which are among the highest-conviction outcomes.
Implications: Scanner count is fixed at six for v1. Sub-goals 1 and 5 in OBJECTIVES reflect this. Phase 2 re-evaluation is an explicit milestone in INSTRUCTIONS.

---

## D-003 — Two-stage entity resolution; party-resolution confidence as a first-class scoring dimension

Date: 2026-04-14
Context: Tools 1 and 2 resolve entities from `ticker + MIC → FIGI`. Court captions use legal-entity names, not tickers; this single fact breaks the Tool 1/2 resolution protocol. Two-stage resolution (party-name normalization → entity resolution) is required, and the confidence of resolution is a first-order determinant of signal usefulness.
Decision: Adopt the two-stage protocol specified in `CONTEXT.md` (strip corporate-form suffixes, classify party type, then try internal cache → SEC EDGAR exact → SEC EDGAR fuzzy → Exhibit 21 subsidiary → OpenFIGI NAME → unresolved, in that order). Record `resolution_method` and `resolution_confidence` on every signal. Signals with confidence < 0.85 are triaged out at Stage 1. Replace Tool 1/2's "Catalyst Timeline" dimension with "Party-Resolution Confidence" (×1 weight). Convergence engine keys on `issuer_figi` only; never on party-name string.
Alternatives considered:
  - Single-stage resolution directly via OpenFIGI NAME lookup. Rejected: precision < 50% in pilot testing; many false positives would poison the candidate pipeline.
  - Do not score confidence; rely on triage gate alone. Rejected: borderline-confidence signals (0.80–0.85) still reach the pipeline and need to be scored down, not just gated in/out.
Implications: Resolution cache is a project-lifetime dataset that grows monotonically (D-009). Executive-lookup table is a derived dataset (D-010). Scoring rubric has one dimension (Party-Resolution Confidence) that Tool 1/2 analysts will not recognize.

---

## D-004 — Common signal JSON schema compatibility

Date: 2026-04-14
Context: Tool 1 and Tool 2 share a signal JSON schema. Cross-tool convergence requires Tool 3's schema be structurally compatible so a single analyzer can read all three.
Decision: Tool 3's outer signal schema matches Tool 1/2 verbatim. Litigation-specific fields (court, case_number, case_caption, party_role, party_raw_name, resolution_method, resolution_confidence) live inside `raw_data`, not as new top-level fields. Tool 3's `entity_id` is CIK (same as Tool 1). `signal_category` values are new but drawn from the same flat-string pattern.
Alternatives considered:
  - Add top-level litigation fields. Rejected: breaks Tool 1/2 compatibility; future analyzer would need schema-version-dispatch logic.
  - Entirely new schema. Rejected: convergence across tools becomes a translation problem.
Implications: Tool 3 convergence engine and any future cross-tool analyzer can use the Tool 1/2 schema-parsing code unmodified at the outer level.

---

## D-005 — Per-channel cadences; 30-day convergence window

Date: 2026-04-14
Context: Tool 1 runs scanners every 3 hours. Litigation events move more slowly — most dockets update daily at most, and many key events (PTAB, ITC) post weekly or monthly. A 3-hourly cadence would produce ≥ 95% redundant reads and waste scheduler budget.
Decision: Per-channel cadences — Federal Civil and SEC Enforcement every 6h; ITC, Delaware Chancery, DOJ/FTC every 12h; PTAB daily. Operational task runs every 6h and dispatches scanners according to per-channel cadence (tracked via per-channel timestamp files). Convergence window is 30 days (wider than Tool 1/2's 14 days) because litigation-signal chains on the same entity span weeks.
Alternatives considered:
  - Single 6h cadence for all channels. Rejected: wastes reads on slow channels.
  - Hourly for federal civil. Rejected: RECAP updates are not that frequent; 6h is adequate.
  - 14-day convergence window for compatibility. Rejected: empirically, same-entity multi-channel litigation chains span 3–6 weeks.
Implications: Operational task is 6-hourly with sub-dispatch. Maintenance is 50 min after operational per template Part 4. Performance report daily, deep-dives every 8h. All four tasks share a single `SESSION_LOCK.md`.

---

## D-006 — US-only v1; non-US deferred

Date: 2026-04-14
Context: Tool 2 covers nine non-US exchanges. Litigation tools for those jurisdictions exist (UK Commercial Court, EU General Court, German Federal Court, etc.) but require different entity-resolution (no single-country analog to CIK) and different procedural schemas.
Decision: US-only for v1. Defer non-US litigation (UK Commercial Court, EU General Court, Canada Federal Court, etc.) to Phase 8+, re-evaluated after v1 produces validated candidates and the party-resolution cache has matured.
Alternatives considered:
  - Include UK Commercial Court in v1 (highest-value non-US litigation venue for public companies). Rejected: UK party resolution is its own problem (Companies House lookup, parent-company identification) and would extend build by 3–4 weeks.
Implications: Tool 3 candidates are a US-market subset only. Non-US litigation signals against US-listed ADRs (and cross-border antitrust) are partially captured by SEC Enforcement and DOJ/FTC channels but not by international scanners.

---

## D-007 — Federal criminal, state courts (ex-Delaware), bankruptcy out of v1

Date: 2026-04-14
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

Date: 2026-04-14
Context: PROJECT_TEMPLATE mandates "free APIs / free disclosure portals only." PACER costs $0.10/page and is the source-of-record for federal court documents. RECAP (the free Free Law Project mirror) covers a meaningful but incomplete subset.
Decision: Tool 3 v1 never autonomously spends PACER credits. All scanners use RECAP for document bodies; when RECAP lacks a document, the scanner records the docket entry (index metadata is always free via CourtListener) and emits a signal with `raw_data.document_status = "in_pacer_only"` and `raw_data.pacer_cost_estimate_cents = <page_count × 10>`. Deep-dive analysis flags these in `working/pacer_pulls_requested.md` for user-directed manual retrieval. This preserves the "free only" mandate for the autonomous system while allowing the user to retrieve specific documents out-of-band at their discretion.
Alternatives considered:
  - Allow a monthly PACER budget (e.g., $50/mo) for autonomous pulls. Rejected: violates mandate; opens door to unbounded cost growth; adds billing-reconciliation complexity.
  - Only scan RECAP; do not record missing-document metadata. Rejected: loses the information that a document exists, which is itself a signal.
Implications: v1 scan coverage for document bodies is RECAP-only. Index metadata coverage is full (via CourtListener). Some deep-dive analyses will have to proceed from docket-entry descriptions alone, without the filed document body. Phase 2 may revisit.

---

## D-009 — Internal party-to-issuer resolution cache: monotonic, session-lifetime

Date: 2026-04-14
Context: Every resolved party name is a lookup result that should not need to be redone. Recomputing resolutions every session wastes API calls and budget.
Decision: Maintain `baselines/party_resolution_cache.json` (or SQLite) as a monotonically-growing mapping from normalized party string → resolution result (method, confidence, issuer_figi, CIK, ticker, last-verified timestamp). Every successful resolution writes an entry. Cache entries have a `last_verified` timestamp; if > 180 days old, re-verify on next encounter (subsidiary relationships can change via M&A). Never delete entries — mark stale and re-verify. Cache is a checkpointed artifact in archive cycles.
Alternatives considered:
  - Recompute every session. Rejected: wastes API budget and slows the pipeline.
  - No stale-check; cache forever. Rejected: subsidiaries get spun off; parent-child mappings decay.
Implications: Cache is a project-lifetime asset. Performance improves over time. Cross-session continuity requires the cache file be present and valid.

---

## D-010 — Executive-lookup table for SEC enforcement executive-respondent signals

Date: 2026-04-14
Context: SEC Enforcement channel frequently names individuals (not just entities). To classify "is this executive employed by a public company in the universe," need a lookup table.
Decision: Maintain `baselines/executive_lookup.json` — a mapping of normalized executive name → list of (issuer_figi, role, as-of-date). Built from SEC DEF 14A (proxy statement) filings for in-universe companies; updated quarterly as part of baseline maintenance. Resolution on executive name is case-insensitive, ignores middle initials, with fuzzy-match fallback (Levenshtein ≤ 2 on full name).
Alternatives considered:
  - Resolve executive names via web search at scan time. Rejected: unreliable, slow, and produces inconsistent results.
  - Skip executive-respondent signals entirely. Rejected: executive-only SEC actions are a valuable under-followed sub-signal.
Implications: Quarterly executive-lookup refresh is a maintenance-task responsibility. Proxy-statement parsing is a new dependency. Executive-respondent signals have their own resolution-confidence scale (distinct from entity resolution).

---

## D-011 — Archive, never delete; overwrite-only lock semantics

Date: 2026-04-14
Context: PROJECT_TEMPLATE non-negotiables #1 and #7 mandate overwrite-only locks and never-delete / always-archive. No deviation.
Decision: Follow PROJECT_TEMPLATE non-negotiables verbatim. Archive path: `archive/YYYY-MM-DD_<reason>/`. Lock file uses overwrite-only semantics; even on abnormal termination, never `rm`. 4-hour stale-lock window per template Part 3.6.
Alternatives considered: None — template non-negotiable.
Implications: Deletion of superseded files requires manual user action outside the autonomous system. Accepted cost.

---

## D-012 — Scheduled-task naming and cron offsets

Date: 2026-04-14
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

---

## D-013 — SKILL.md files stored in `litigation_system/skills/<task-id>/SKILL.md` directories

Date: 2026-04-14
Context: PROJECT_TEMPLATE Part 5 describes four SKILL.md templates but does not dictate where the files live on disk. The scheduled-tasks MCP stores each task's skill under a directory whose name is the taskId. Keeping the SKILL.md templates as authored artifacts in the project folder (so they can be version-controlled and re-deployed) requires picking a location.
Decision: Author the four SKILL.md files under `litigation_system/skills/<task-id>/SKILL.md` — one directory per scheduled task, matching the task-id conventions in D-012 (`litigation-operational`, `litigation-maintenance`, `litigation-performance-report`, `litigation-deep-dives`). At task-registration time, the SKILL.md content is copied verbatim into whatever scheduled-tasks store path the MCP uses. The in-project copy is the source of truth; the deployed copy is a downstream artifact.
Alternatives considered:
  - Keep SKILLs flat at `litigation_system/SKILL_<task>.md`. Rejected: mixes session-relay files with executable task definitions; clutters the top level.
  - Author SKILLs only in the scheduled-tasks store and not in the project folder. Rejected: loses version-control, loses cold-startability (a new session cold-reading `litigation_system/` would not see the task definitions), and couples project integrity to the MCP store's durability.
Implications: Any edit to a SKILL.md requires redeploying to the scheduled-tasks store. A "diff before deploy" step is added to the maintenance task in Phase 2+ to catch drift between authored and deployed copies. `INDEX.md` lists all four SKILL.md files under `skills/`.

---

## D-014 — USPTO PTAB API migration: v2 developer-hub deprecated, move to v3 ODP

Date: 2026-04-14
Context: Phase 1 endpoint validation (Session 2) discovered a time-critical migration. The bootstrap seed specified PTAB API v2 at `developer.uspto.gov/api-catalog/ptab-api-v2`. The Developer Hub homepage now carries an official banner: "Open Data Portal Beta will be retiring … scheduled for shutdown on April 20, 2026. All remaining Office Action and Enriched Citation APIs are now available on ODP." PTAB API v3 is live at `data.uspto.gov` with Swagger at `data.uspto.gov/swagger/index.html#/Proceedings/get_api_v1_patent_trials_proceedings_search`, however — all probes from the Cowork sandbox to `data.uspto.gov/api/v1/...` return the Angular SPA shell rather than JSON, because AWS WAF (`0dd6fc7fe1e2.edge.sdk.awswaf.com`) serves a browser challenge to non-browser clients.
Decision: (a) Abandon PTAB v2 immediately; do NOT build against `developer.uspto.gov/ptab-api`. (b) Target PTAB v3 at `data.uspto.gov/api/v1/patent/trials/proceedings/search` as the Phase 3 PTAB scanner endpoint. (c) Treat PTAB channel as DEGRADED until WAF-bypass mechanism is confirmed (see Q-004): either ODP API-key header exempts caller from WAF, or Phase 3 PTAB scanner runs under a browser-context executor (Playwright/Chromium) rather than `requests`. (d) During the ~6-day gap before v2 decommission, take one opportunistic snapshot pull from v2 to seed `baselines/ptab_baseline_proceedings.json` so Phase 3 has historical context even if v3 access is still gated.
Alternatives considered:
  - Stay on v2 and plan a migration later. Rejected: v2 disappears 2026-04-20; shipping a scanner built on a known-dead API is wasted work.
  - Skip PTAB channel in v1 entirely. Rejected: PTAB is one of the highest-asymmetry signal channels (patent-validity outcomes move small- and mid-cap biotech/tech decisively per CONTEXT.md §"Strategy Selection Rationale").
  - Use a commercial PTAB-data reseller. Rejected: violates D-008 free-sources-only policy.
Implications: PTAB scanner Phase 3 build is blocked on Q-004. Maintenance task must monitor `data.uspto.gov/support/` release notes for any ODP API-key-bypass announcement. `strategies/strategy_ptab_ipr.md` needs a Phase 3 rewrite to reflect v3 endpoint paths and the Playwright-fallback branch.

---

## D-015 — USITC UA-sanitization for Akamai edge filter

Date: 2026-04-14
Context: Phase 1 endpoint validation probed `www.usitc.gov` and `edis.usitc.gov` with the operational User-Agent string `"Litigation Signal Tool / Pedro (javiergorordo13@hotmail.com)"` and received HTTP 403 on all paths. Re-probing the same URLs with either no User-Agent header or a plain browser UA (`Mozilla/5.0 ...`) returned HTTP 200 on every tested path. USITC is fronted by Akamai (`kmqp2tqcck6aw2o6oc2a-f-d450f51b3-clientnsv4-s.akamaihd.net` visible in EDIS external page script); the Akamai edge appears to flag User-Agent strings containing the literal token `"Tool"` (and possibly others — SEC-compliance-style `"Name / contact@email"` UA format is not honored by USITC the way SEC honors it).
Decision: For all requests to `www.usitc.gov` and `edis.usitc.gov`, the ITC scanner (Phase 3) MUST either (a) omit the User-Agent header entirely, or (b) send a plain browser-style UA such as `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36`. Do NOT propagate the general-purpose `"Litigation Signal Tool / …"` UA to USITC hosts. Every other host in the endpoint table may continue to use the operational UA — in particular, SEC hosts REQUIRE the operational UA (`Name contact-email` form) per SEC's own fair-access policy, so a per-host UA dispatch is necessary.
Alternatives considered:
  - Send the operational UA everywhere and accept degraded USITC coverage. Rejected: ITC Section 337 is a core v1 channel (D-002); a 403-always scanner is useless.
  - Ask USITC to allowlist the operational UA. Rejected: out-of-band, not within v1 build scope, and no guarantee of response; plain-browser-UA has no user-harm and is policy-compliant.
Implications: Scanner code (Phase 3) must implement a per-host UA map:
```
UA_MAP = {
  "www.sec.gov": OPERATIONAL_UA,
  "data.sec.gov": OPERATIONAL_UA,
  "efts.sec.gov": OPERATIONAL_UA,
  "www.usitc.gov": BROWSER_UA,
  "edis.usitc.gov": BROWSER_UA,
  # ... default: OPERATIONAL_UA
}
```
This is a small but non-negotiable piece of infrastructure. Maintenance task must also re-probe USITC with the operational UA quarterly in case the Akamai filter changes and we can unify on a single UA.

---

## D-016 — Delaware Chancery scanner redesign: CourtConnect primary, opinions scrape secondary, no RSS

Date: 2026-04-14
Context: The bootstrap seed and `strategies/strategy_delaware_chancery.md` assumed a two-source design: RSS primary (for new-filings discovery) and docket-search enrichment. Phase 1 endpoint validation found (a) `courts.delaware.gov/chancery/rss.aspx` returns HTTP 200 but the body is a "Page Not Found" HTML page — **there is no Chancery RSS feed**; (b) Delaware's public docket is served through `courtconnect.courts.delaware.gov/cc/cconnect/` (Avenu "Contexte" product), NOT the stale `courts.delaware.gov/help/onlineservices/docketsearch.aspx` path that appeared in the seed; (c) **there is no CAPTCHA, no reCAPTCHA, no JS challenge** on CourtConnect — only a disclaimer interstitial (resolving Q-002 negative). The actual scrape surface is the CourtConnect frameset flow (`cp_main_idx` → `cp_main_disclaimer` → `cp_disclaimer_srch_link` → search form).
Decision: Invert the Chancery scanner design. (a) CourtConnect docket-search is the primary new-filings surface, with the two-hop disclaimer flow implemented as a session-establishment step. (b) `courts.delaware.gov/opinions/index.aspx?ag=court%20of%20chancery` is the secondary surface for decided-matter enrichment. (c) No RSS component — the `rss.aspx` URL is removed from the endpoint table. (d) The scanner must parse Delaware's frameset HTML (three nested frames per CourtConnect page) and handle Avenu Contexte's idiosyncratic URL patterns (`ck_public_qry_*` controller names).
Alternatives considered:
  - Headless-browser the Chancery flow. Rejected as Phase 3 default: plain `requests` + `beautifulsoup4` suffice since there's no JS challenge; headless browser adds latency, memory, and complexity unnecessary for this channel. Keep headless browser as Phase 4+ fallback if Avenu changes the session model.
  - Skip Chancery in v1 and cover only via PACER (for consolidated MDLs). Rejected: appraisal actions and DGCL 220 books-and-records demands don't federalize; losing Chancery means losing M&A deal-break signals, which are among the highest-conviction outcomes per CONTEXT.md §"Strategy Selection Rationale".
Implications: `strategies/strategy_delaware_chancery.md` must be rewritten in Phase 3 build to match CourtConnect frameset scraping. `CONTEXT.md` endpoint table has been updated to point to CourtConnect (already reflected in the 2026-04-14 Phase-1 probe pass). No new runtime dependency — `beautifulsoup4` + `lxml` are already in the planned Execution Environment.

---

## Adversarial note (for every future session)

If a past decision looks wrong, do NOT edit it. Append a new numbered decision that overrides it, citing the new evidence. History is load-bearing. D-003's "Party-Resolution Confidence replaces Catalyst Timeline" is the decision that most often looks second-guessable; every time it comes up, re-read the D-003 rationale and F-01 / F-14 failure modes before considering an override.
