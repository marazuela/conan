# PROGRESS_LOG — Litigation & Docket Signal System (Tool 3)

> Append-only. One block per session. Never delete. History is load-bearing.

Session blocks are chronological. Each block ends with a terse 1-line "Net state" that future-you can skim without reading the full block.

---

## Session 1 — 2026-04-14 — INSTANTIATION (Phase 0)

**Mode:** Interactive (user-present), one-shot build session.
**Operator:** Pedro (user), Claude agent.
**Scope:** Instantiate Tool 3 from `project set up template/litigation_tool_bootstrap/` into `litigation_system/` + `reporting_layer/`. Author Phase 0 relay files + Phase 1 SKILL.md set. No scanner code, no endpoint probes.

### What was done

- Read all ten bootstrap seed files under `project set up template/litigation_tool_bootstrap/`:
  `LITIGATION_BOOTSTRAP_README.md`, `LITIGATION_OBJECTIVES.md`, `LITIGATION_CONTEXT.md`, `LITIGATION_STRATEGIES.md`, `LITIGATION_SCORING.md`, `LITIGATION_DECISIONS_SEED.md`, `LITIGATION_PHASING.md`, `LITIGATION_FAILURE_MODES.md`.
- Read governing template: `project set up template/PROJECT_TEMPLATE.md` (566 lines) and `_scratch_diagram/tool_architecture_diagram.md`.
- Scaffolded folder tree: `litigation_system/{framework,strategies,tools,baselines,candidates,scan_results,working,skills,archive}` and `reporting_layer/{performance_reports,litigation_briefs}`.
- Authored relay files in `litigation_system/`:
  - `SESSION_LOCK.md` (UNLOCKED initial state)
  - `PROJECT_INSTRUCTIONS.md` (charter, 12-point self-review, standing question)
  - `OBJECTIVES.md` (primary goal, mandate, 6-channel scope, sub-goals, success criteria — adapted from seed)
  - `CONTEXT.md` (endpoint table all `⚠️ UNVERIFIED`, entity-resolution protocol, signal schema)
  - `DECISIONS.md` (D-000 through D-012 verbatim from seed + new D-013 on SKILL location)
  - `OPEN_QUESTIONS.md` (Q-001 judge-effect, Q-002 Delaware CAPTCHA, Q-003 CourtListener rate limits)
  - `INSTRUCTIONS.md` (cold-start protocol, 11-stage pipeline, execution model, scheduled-task table, tool-validation protocol)
  - `SESSION_STATE.md` (Phase 0 complete snapshot, Phase 1 priority queue)
  - `PROGRESS_LOG.md` (this file)
- Initialized `reporting_layer/litigation_briefs/index.json` as empty array.
- Authored four SKILL.md files under `litigation_system/skills/<task-id>/SKILL.md` (per D-013).
- Authored `framework/scoring_system.md` (7-dim rubric) and `framework/candidate_template.md`.
- Authored six per-channel strategy files in `strategies/`.
- Authored `README.md` and `INDEX.md` last.

### Decisions made

- **D-013 NEW** — SKILL.md files authored under `litigation_system/skills/<task-id>/SKILL.md` (not flat at top level). Rationale: version control, cold-startability, separation of relay files from executable task definitions.
- Carried **D-000 through D-012 verbatim** from seed (all dated 2026-04-14). No deviations.
- Added `reportlab` and `python-docx` to the pip install line in CONTEXT.md execution-environment block, since Phase 6 deliverables require direct PDF/DOCX generation.

### Questions raised (not resolved)

- **Q-001** — Judge-effect modeling for Signal Strength (deferred to Phase 8+).
- **Q-002** — Delaware Chancery CAPTCHA feasibility from Cowork sandbox (resolved by Phase 1 live probe).
- **Q-003** — CourtListener free-tier rate limits at steady state (resolved by Phase 1 + Phase 3 observation).

### Verification steps completed

- Cross-read `INSTRUCTIONS.md` cold-start protocol against `PROJECT_INSTRUCTIONS.md` Session Continuity Protocol — aligned.
- Cross-checked `DECISIONS.md` D-012 scheduled-task names against `skills/` directory names — match exactly (`litigation-operational`, `litigation-maintenance`, `litigation-performance-report`, `litigation-deep-dives`).
- Verified `CONTEXT.md` execution-environment pip line names match imports used in SKILL.md task scripts and in strategy files.
- Verified `OPEN_QUESTIONS.md` format matches template Part 3.4 exactly.
- Confirmed `INDEX.md` enumerates every file actually created (no phantom references).

### Artifacts NOT produced (by design)

- Scanner code (`tools/*.py`) — Phase 3+.
- Scheduled-task registration — Phase 5.
- Endpoint probes — Phase 1 (next session).
- Baselines (`baselines/*.json`) — Phase 2.

### Net state

Phase 0 complete. Ten relay files present, folder tree scaffolded, SKILL.md set authored, endpoint table exists but every row still `⚠️ UNVERIFIED`. Zero tools built. Next session = Phase 1 endpoint validation.

---

## Session 2 — 2026-04-14 — PHASE 1 ENDPOINT VALIDATION

**Mode:** Interactive (Pedro directing), continuation of Session 1 under a fresh SESSION_LOCK.
**Operator:** Pedro (user), Claude agent.
**Scope:** Live-probe every endpoint in `CONTEXT.md` from the Cowork sandbox. Update the Status column in place. Resolve Q-002 and Q-003 where possible. Append decisions for any non-trivial findings.

### What was done

- Overwrote `SESSION_LOCK.md` from UNLOCKED → LOCKED (session scope: "Phase 1 endpoint validation only. Read/write to litigation_system/. No scanner code, no scheduled-task registration.").
- Re-read cold-start files: `README.md`, `INDEX.md`, two of four SKILLs (deep-dives, performance-report).
- Ran ~40 HTTP probes across all six channels + Tool 1 inherited endpoints + four party-resolution sources. Raw results summarized below.
- Updated `CONTEXT.md` endpoint tables (Primary + Support + Party-resolution) in place with probe date `2026-04-14` and VERIFIED / WAF-GATED / UA-SENSITIVE / BLOCKED annotations.
- Resolved Q-002 with a concrete answer (no CAPTCHA on Delaware CourtConnect; scanner design must invert — see D-016).
- Partially resolved Q-003 (CourtListener v4 root reachable; rate-limit sufficiency still empirical).
- Opened Q-004 (PTAB v3 WAF-challenge bypass) and Q-005 (EDIS REST spec PDF re-pull).
- Appended D-014 (PTAB v2→v3 migration, 2026-04-20 deadline), D-015 (USITC UA-sanitization), D-016 (Delaware Chancery scanner redesign) to `DECISIONS.md`.
- Rewrote `SESSION_STATE.md` to reflect Phase 1 complete, Phase 2 next, plus new tool-health row for `ptab_baseline_proceedings.json` opportunistic pull.

### Probe results (abridged — full curl transcript in session log)

| Endpoint | Probe | Result |
|----------|-------|--------|
| `courtlistener.com/api/rest/v4/` | GET | HTTP 200, full endpoint catalog returned |
| `usitc.gov/*` (operational UA) | GET | HTTP 403 on every path |
| `usitc.gov/*` (no UA or browser UA) | GET | HTTP 200; `/news_releases` enumerates 2026 press releases |
| `edis.usitc.gov/external/` | GET | HTTP 200; API button links to `.../edis_data_web_service_guide.pdf` (Q-005) |
| `edis.usitc.gov/api/v1/*` | GET | HTTP 404 — Swagger not at that path |
| `developer.uspto.gov/api-catalog` | GET | HTTP 200; banner: "ODP Beta will be retiring … April 20, 2026" (D-014) |
| `data.uspto.gov/api/v1/patent/trials/proceedings/search` | GET | HTTP 200 but body is Angular SPA + AWS WAF challenge script (Q-004) |
| `data.uspto.gov/swagger/index.html`, `/v3/api-docs` | GET | HTTP 200 but serves SPA shell (WAF-gated) |
| `courtconnect.courts.delaware.gov/cc/cconnect/ck_public_qry_main.cp_main_idx` | GET | HTTP 200, HTML frameset (3 frames); no CAPTCHA (Q-002 answered) |
| `courtconnect.../cp_main_disclaimer?search_option=party` | GET | HTTP 200, disclaimer frameset with link to search form |
| `courts.delaware.gov/chancery/rss.aspx` | GET | HTTP 200 body = "Page Not Found" HTML (**no RSS feed exists**) |
| `courts.delaware.gov/opinions/` | GET | HTTP 200 |
| `sec.gov/cgi-bin/browse-edgar?...&output=atom` | GET | HTTP 200 atom feed (both `type=` blank and `type=8-K` after UA) |
| `sec.gov/litigation/litreleases.htm` | GET | HTTP 301 → reachable landing |
| `efts.sec.gov/LATEST/search-index?q=litigation&...&forms=8-K` | GET | HTTP 200 |
| `efts.sec.gov/LATEST/search-index?q="Exhibit+21"&forms=10-K` | GET | HTTP 200 (Exhibit-21 corpus queryable) |
| `data.sec.gov/submissions/CIK0000320193.json` | GET | HTTP 200 (re-verified from Tool 1) |
| `api.openfigi.com/v3/mapping` | GET / POST | HTTP 405 on GET, HTTP 200 on POST with `[{idType,idValue,exchCode}]` body |
| `justice.gov/atr/press-releases` | GET | HTTP 200 |
| `justice.gov/feed/press_releases/rss.xml`, `/rss/atr` | GET | HTTP 404 (no ATR-specific RSS) |
| `justice.gov/news/rss` | GET | HTTP 200 (global news RSS; filter required) |
| `ftc.gov/feeds/press-release-competition.xml` | GET | HTTP 200 RSS |
| `ftc.gov/feeds/press-release.xml` | GET | HTTP 200 RSS |
| `ftc.gov/news-events/news/press-releases` | GET | HTTP 200 |
| `wikidata.org/w/api.php?action=wbsearchentities&search=Apple%20Inc` | GET | HTTP 200 JSON |
| `query.wikidata.org/sparql?query=...` | GET | HTTP 200 |

### Decisions made

- **D-014 NEW** — USPTO PTAB API migration from v2 (Developer Hub, dies 2026-04-20) to v3 (ODP at `data.uspto.gov`). PTAB scanner treated as degraded until Q-004 resolves WAF bypass. Opportunistic v2 snapshot pull before 2026-04-20 added as a Phase 1b sub-task.
- **D-015 NEW** — Per-host UA dispatch required. USITC hosts (`www.usitc.gov`, `edis.usitc.gov`) get a plain browser UA; SEC hosts keep the operational `"Name contact-email"` UA. Phase 3 scanners must implement a host → UA map.
- **D-016 NEW** — Delaware Chancery scanner redesign: CourtConnect primary (frameset flow, no CAPTCHA), opinions scrape secondary, no RSS (the planned `chancery/rss.aspx` returns a 404 body). `strategies/strategy_delaware_chancery.md` to be rewritten in Phase 3.
- D-000 through D-013 remain unchanged.

### Questions raised (not resolved)

- **Q-004 NEW** — PTAB v3 WAF-challenge bypass feasibility. Time-critical: v2 dies 2026-04-20.
- **Q-005 NEW** — EDIS REST spec PDF follow-through (`.pdf` URL returned 301; the real spec path needs `-L` probe).
- **Q-003 partial** — CourtListener v4 reachable; steady-state rate-limit verdict deferred to Phase 3.

### Questions answered

- **Q-002 ANSWERED** — Delaware's public docket is CourtConnect (Avenu Contexte product), not the stale `docketsearch.aspx`. No CAPTCHA, no JS challenge — only a disclaimer interstitial. Scanner design inverted per D-016.

### Verification steps completed

- Counter-probed USITC with three different UA treatments (operational, browser, none) to confirm the Akamai filter before committing D-015 to writing.
- Counter-probed OpenFIGI with both GET (405) and POST (200) to rule out a Tool-1-era regression.
- Cross-read `chancery/rss.aspx` body (not just HTTP code) before declaring "no RSS feed"; confirmed body is "Page Not Found" HTML despite 200 status.
- Confirmed Developer-Hub decommissioning banner is official (not a stale page) by reading the API-catalog page Drupal-served content directly.
- Cross-checked that SEC operational UA still works after `www.sec.gov` browse-edgar `type=8-K` variant had returned HTTP 000 on first probe (transient — 200 on retry).

### Artifacts NOT produced (by design)

- Scanner code (`tools/*.py`) — Phase 3+.
- `baselines/` files — Phase 2 (next session).
- Scheduled-task registration — Phase 5.
- Playwright/headless-browser PTAB fetcher — blocked on Q-004 resolution.
- `strategies/strategy_delaware_chancery.md` rewrite — Phase 3 (at scanner-build time, per D-016).

### Net state

Phase 1 complete. 12/14 probed endpoints VERIFIED; 2 degraded (PTAB v3 WAF-gated, EDIS REST spec needs re-pull). Q-002 answered negative (no CAPTCHA); Q-003 partial; Q-004 + Q-005 newly open. D-014, D-015, D-016 appended. Next session = Phase 2 party-resolution cache bootstrap; opportunistic side-quest before 2026-04-20 is a PTAB v2 snapshot pull for historical baseline.

---

## Session 3 — 2026-04-14 — PHASE 2 (PARTIAL) PARTY-RESOLUTION SCAFFOLD

**Mode:** Interactive (Pedro directing), continuation under fresh SESSION_LOCK (`2026-04-14-phase2-party-resolution-bootstrap`).
**Operator:** Pedro (user), Claude agent.
**Scope:** Phase 2 party-resolution bootstrap. Write scope limited to `tools/`, `baselines/`, and relay files. Opportunistic Phase 1b side-quest: PTAB v2 snapshot pull before decommission 2026-04-20.

### What was done

- Overwrote `SESSION_LOCK.md` UNLOCKED → LOCKED for Phase 2 at 2026-04-14T13:00:00Z.
- Authored `tools/party_resolver.py` (~450 lines) implementing the Stage 1 normalizer and the full Stage 2 resolution chain per CONTEXT.md §"Entity Resolution Protocol" and D-003:
  - Stage 1 is a pure function (`normalize_party()`) — no I/O, fully offline-testable.
  - Stage 2 (`resolve()`) walks cache_exact (1.00) → sec_edgar_exact (0.95) → sec_edgar_fuzzy (0.80) → exhibit21_direct (0.90) / indirect (0.75) → openfigi_name (≤0.70) → unresolved.
  - Write-through caching is gated at confidence ≥0.85 (never caches fuzzy or openfigi_name), per D-009.
  - Per-host UA dispatch honored: operational UA `"Litigation Signal Tool contact-javiergorordo13@hotmail.com"` for SEC hosts, neutral UA elsewhere (D-015).
  - SEC fair-access throttle (`SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.15`) built into `_sec_get()`.
  - `Resolution.as_signal_raw_data()` emits the `raw_data` triple that matches INSTRUCTIONS.md §3 signal schema.
  - CLI wrapper with `--offline` and `--no-openfigi` flags for future debugging.
- Authored `tools/test_party_resolver.py`: offline unit tests for Stage 1 covering corporate-suffix stripping (English + European forms), stacked suffixes, government classification (5 canonical agencies + state-of), individual classification (explicit-role + bare-name heuristic), Unicode/whitespace normalization, the signal raw_data projection, and the Holdings-caveat regression guard from D-003.
- Scaffolded `baselines/party_resolution_cache.json` with schema-version 1, inline `_entry_schema` documentation, empty `entries: {}` — ready for the cache write-back step of `resolve()` to populate.
- Scaffolded `baselines/exhibit21_subsidiary_table.json` with schema-version 1, inline `_entry_schema` + `_known_caveats`, empty `entries: {}` — ready for the Phase 2-continuation population pass.

### Decisions made

- No new D-entries. The scope of this session (scaffold-only) did not produce any architectural decision that wasn't already covered by D-003, D-009, D-015.

### Questions raised

None new. Q-003 (CourtListener rate-limit verdict), Q-004 (PTAB v3 WAF bypass), Q-005 (EDIS spec re-pull) remain open from Session 2.

### Verification steps completed

- Cross-read `party_resolver.py` resolution chain against CONTEXT.md §"Entity Resolution Protocol" and the confidence bands in D-003 — every method→confidence pair matches.
- Cross-read Stage 1 suffix regex against the worked examples in CONTEXT.md (`Apple Inc.` → `apple`, `Acme Holdings, Inc.` → `acme`) — matches.
- Confirmed `as_signal_raw_data()` emits exactly the three fields `party_raw_name`, `resolution_method`, `resolution_confidence` that INSTRUCTIONS.md §3 and the signal schema in CONTEXT.md require inside `raw_data` (other signal fields are composed by the scanner, not the resolver).
- Confirmed cache write-back is gated at `confidence >= 0.85` (inspected `_save_cache` + the call site in `resolve()`), per D-009.
- Confirmed per-host UA dispatch: SEC hosts get `OPERATIONAL_UA`, OpenFIGI gets `NEUTRAL_UA`, no SEC UA leaks to OpenFIGI or vice-versa. D-015 compliance verified by code read.
- Verified test file runs without `requests` / `rapidfuzz` imports at the module level — Stage 1 remains testable when those packages are absent (lazy imports inside Stage 2 helpers).
- Verified baseline JSON files parse as valid JSON (schema doc inlined; no trailing commas; `entries: {}` present).

### Artifacts NOT produced (by design or by block)

- `baselines/exhibit21_subsidiary_table.json` is EMPTY of rows. Target population (top-100 S&P 500 10-Ks via `efts.sec.gov/LATEST/search-index?q="Exhibit+21"&forms=10-K`) requires live HTTP — **blocked by sandbox unavailability this session** (two workspace-start attempts returned "Workspace unavailable. The isolated Linux environment failed to start").
- `baselines/party_resolution_cache.json` is EMPTY of entries. 20 hand-validated seed cases for smoke-test require live EDGAR + OpenFIGI calls — **blocked by sandbox**.
- `tools/test_party_resolver.py` was written but NOT run. Sandbox outage prevents executing it this session. The file itself is structurally clean (stdlib-only, no external deps) and should run green when sandbox returns.
- Stage 2 end-to-end smoke test — **blocked by sandbox**.
- PTAB v2 snapshot pull for `baselines/ptab_baseline_proceedings.json` (Phase 1b side-quest, D-014 clause (d)) — **blocked by sandbox**. Critical: PTAB v2 dies 2026-04-20 (6 days away). Documented as the top-priority item for next session IF sandbox is back.
- `tools/build_exhibit21_table.py` (the population script referenced in the exhibit21 skeleton's `_population_status`) — not authored; deferred to next session when live HTTP is available for iterative development.
- Scanner code (`tools/*_scanner.py`) — Phase 3+.

### Sandbox outage — honest accounting

The Cowork bash sandbox was unavailable for the duration of this session. Two attempts to start it returned the same "Workspace unavailable" error. Every file-write action (party_resolver.py, both baseline JSONs, test_party_resolver.py, this log entry) succeeded because they use the Write/Edit file tools, not bash. But every action requiring HTTP (live EDGAR calls to build the Exhibit-21 table, live OpenFIGI calls to smoke-test Stage 2, live USPTO calls to grab a v2 snapshot before decommission) was blocked.

Phase 2 is therefore **PARTIAL, NOT COMPLETE**. The code path is structurally correct and offline-testable, but no live-data validation has occurred. Phase 3 (scanner builds) MUST NOT start until:
1. `test_party_resolver.py` has been run and passes (offline check).
2. Stage 2 has been smoke-tested against live EDGAR + OpenFIGI for at least the 20 seed cases, with confidence distribution recorded.
3. At least a partial Exhibit-21 table exists (top-25 S&P 500 is the minimum viable floor; top-100 is target).

### Net state

Phase 2 PARTIAL. Party resolver coded end-to-end with offline Stage 1 tests; both baseline JSONs exist as empty schemas; no live validation performed due to sandbox outage. PTAB v2 snapshot pull missed this session; next session has 5 days before v2 decommissions. No new D-entries, no new Q-entries. Zero scanners built. Next session = retry Phase 2 live-population (Exhibit-21 build + cache seed + Stage 2 smoke) + PTAB v2 snapshot pull (time-critical) + Q-005 EDIS spec re-pull.

---

## Session 4 — 2026-04-14 — OFFLINE SCRIPT PREP (narrow re-lock)

**Mode:** Interactive (Pedro directing), narrow re-lock after Session 3 closed.
**Scope:** Author-only, no execution. Front-load the two scripts next session will need to run the moment sandbox returns, to preserve the narrow 5-day window before PTAB v2 decommission. Write scope limited to `tools/`.

### What was done

- Re-probed sandbox (still unavailable — same "Workspace unavailable" error as Session 3).
- Re-locked `SESSION_LOCK.md` for this narrow scope at 2026-04-14T13:30:00Z.
- Authored `tools/ptab_v2_snapshot.py`: one-shot paginated pull of the USPTO Developer Hub PTAB v2 proceedings endpoint into `baselines/ptab_baseline_proceedings.json`. Resume-friendly, self-throttled, content-type-guarded against the post-decommission HTML shell. CLI: `--limit` (test mode), `--resume` (continue partial pull).
- Authored `tools/build_exhibit21_table.py`: walks SEC EDGAR for each seed CIK, finds the most-recent 10-K, locates the Exhibit-21 attachment via `index.json`, parses it (HTML-table-first, plaintext fallback), normalizes each subsidiary through `party_resolver.normalize_party()`, and merges into `baselines/exhibit21_subsidiary_table.json` with collision handling (same-key different-parent → list value). CLI: `--top N`, `--cik`, `--reset`, `--dry-run`. Seed CIK list embedded; duplicates tolerated by de-dupe pass.

### Decisions made

None. Both scripts implement the protocol already codified in D-003, D-009, D-014, D-015.

### Questions raised

None.

### Verification steps completed

- Cross-read `ptab_v2_snapshot.py` against D-014 clause (d): endpoint URL matches `developer.uspto.gov/ptab-api/proceedings`, UA matches D-015 operational form, resume cursor is `_last_page_fetched`, output schema matches the other baseline JSONs (`_schema_version`, `_description`, `entries` list).
- Cross-read `build_exhibit21_table.py` against CONTEXT.md §"Entity Resolution Protocol" — it imports `party_resolver.normalize_party` rather than re-implementing normalization, so any Stage 1 change propagates without drift. Collision semantics (list-valued keys) match the resolver's expected triage behavior.
- Verified both scripts lazy-import `requests` inside the action functions, so `import` at file scope does not fail offline. Next session can even run `python tools/ptab_v2_snapshot.py --help` to sanity-check argparse without any network.
- Verified neither script writes to `baselines/` at module-import time — writes only happen inside `build()` / `pull()`, gated by `--dry-run` where applicable.

### Artifacts NOT produced (by design)

- No scanner code (Phase 3+). F-05 respected.
- No live HTTP calls. Sandbox still down.
- No new baseline rows. Both target JSON files remain schema-only empty — population is an execution-time action, not an authoring action.
- No changes to `CONTEXT.md`, `DECISIONS.md`, `OPEN_QUESTIONS.md`. No new facts learned this session warrant them.

### Net state

Two new scripts exist under `tools/`: `ptab_v2_snapshot.py` and `build_exhibit21_table.py`. Neither has been executed. Sandbox still unavailable. Phase 2 remains PARTIAL. The critical 5-day PTAB v2 window is unchanged — but next session can now `python tools/ptab_v2_snapshot.py` immediately instead of spending cycles authoring. Session lock released UNLOCKED.

---

