# SESSION_STATE — Litigation & Docket Signal System (Tool 3)

> Rewritten every session. Not a log — a snapshot of where things stand RIGHT NOW. History lives in `PROGRESS_LOG.md`.

Last updated: 2026-04-14 (Session 4 — offline script prep; sandbox still down)

---

## TOP HEADLINE

**Phase 2 PARTIAL. `tools/party_resolver.py` is coded end-to-end (Stage 1 + Stage 2 resolution chain) with offline unit tests at `tools/test_party_resolver.py`. Both baseline JSONs (`party_resolution_cache.json`, `exhibit21_subsidiary_table.json`) exist as schema-only empty files. NO live validation occurred: the Cowork bash sandbox was unavailable this entire session. PTAB v2 snapshot side-quest was NOT attempted — critical, 5 days left before v2 decommission (2026-04-20). Next session priority: retry sandbox, run offline tests, populate Exhibit-21 table, seed cache with 20 smoke-test cases, pull PTAB v2 snapshot.**

---

## Current Phase

**Build / Phase 2 — PARTIAL (scaffold complete, live population blocked).**
- `tools/party_resolver.py` authored: Stage 1 normalizer (pure function), Stage 2 resolver (5-method fallback chain: cache → EDGAR exact → EDGAR fuzzy → Exhibit-21 → OpenFIGI NAME), HTTP plumbing with SEC fair-access throttle, per-host UA dispatch per D-015, write-through cache gated at confidence ≥0.85 per D-009.
- `tools/test_party_resolver.py` authored: offline unit tests for Stage 1 covering corporate-suffix stripping (English + European forms), stacked suffixes, government/individual classification, Unicode normalization, signal-schema projection, Holdings caveat regression guard. Not yet executed (sandbox outage).
- `baselines/party_resolution_cache.json` scaffolded: schema-version 1 + `_entry_schema` + empty `entries: {}`. 20 hand-validated seed cases pending.
- `baselines/exhibit21_subsidiary_table.json` scaffolded: schema-version 1 + `_entry_schema` + `_known_caveats` + empty `entries: {}`. Top-100 S&P 500 10-K population pending.
- Stage 2 has been code-reviewed but NOT smoke-tested against live EDGAR or OpenFIGI.

**Next: Build / Phase 2 — live-data completion.** Before Phase 3 scanner builds can begin, the following MUST be done (with a working sandbox):
1. Run `tools/test_party_resolver.py` — expected green (offline-only).
2. Author and run `tools/build_exhibit21_table.py` — populate `exhibit21_subsidiary_table.json` with top-100 S&P 500 constituent subs (minimum viable floor: top-25).
3. Hand-curate 20 seed cases in `party_resolution_cache.json` — S&P 500 majors, wholly-owned subs (Exhibit-21 path), plus individual/government negative coverage.
4. Run Stage 2 end-to-end smoke test; record pass/fail and confidence distribution in a new PROGRESS_LOG block.
5. Time-critical side-quest: PTAB v2 snapshot pull for `baselines/ptab_baseline_proceedings.json` before **2026-04-20**.

---

## Active Work Units

None. No candidates exist. No scanners built. The pipeline is cold.

---

## Watchlist

Empty. Watchlist populates once Phase 4+ produces convergences that don't clear the 28-point Immediate threshold but score 22–27 (per scoring rubric bands).

---

## Future Pipeline

| Phase | Scope | Gate to start |
|-------|-------|---------------|
| 1 | Endpoint validation | ✅ COMPLETE 2026-04-14 |
| 2 | Party-resolution cache bootstrap | ⚠️ PARTIAL 2026-04-14 (code done; live data blocked by sandbox) |
| 3 | Scanner builds — federal_civil + SEC_enforcement first | Phase 2 live-data complete (not yet) |
| 4 | Remaining four scanners + convergence engine + scoring (PTAB pending Q-004; Chancery per D-016) | Phase 3 complete |
| 5 | Scheduled-task registration + 7-day autonomous burn-in | Phase 4 complete |
| 6 | Reporting layer | Phase 5 complete |
| 7 | Kill-condition monitor + maintenance audits | Phase 6 complete |
| 8+ | Non-US, bankruptcy, judge-effect scoring (Q-001) | Post-v1 |

---

## Active Warnings

- **SANDBOX OUTAGE** — Cowork bash sandbox returned "Workspace unavailable" for the full duration of Session 3. Two retries, same error. This blocks all live HTTP — no pip-run tests, no EDGAR calls, no OpenFIGI calls, no USPTO snapshot. Next session's first action: re-probe sandbox health BEFORE committing to a scope.
- **PTAB v2 DECOMMISSION — 5 DAYS** — USPTO Developer Hub PTAB v2 retires 2026-04-20. Phase 1b side-quest snapshot pull was not attempted this session. If sandbox stays down through 2026-04-20, the v2 historical baseline is lost permanently; Phase 4 PTAB scanner must then work entirely from v3 (still WAF-gated per Q-004) or skip historical grounding.
- **Q-001** — Judge-effect scoring (Phase 8+). Unchanged.
- **Q-003** — CourtListener v4 live; rate-limit sufficiency pending Phase 3 empirical test. Unchanged.
- **Q-004** — USPTO PTAB v3 WAF-gated. PTAB scanner (Phase 4) blocked until bypass is resolved. Unchanged; escalating as v2 deadline approaches.
- **Q-005** — EDIS REST spec PDF re-pull needed (`-L` flag follow-through). Unchanged.

See `OPEN_QUESTIONS.md` for full context on Q-entries. See `DECISIONS.md` D-014 for PTAB migration context.

---

## Next Session Priority Queue

Priority order:

1. **Sandbox health check** (FIRST action — `mcp__workspace__bash` with a trivial command). If still down, fall back to offline-doable maintenance only and STOP pushing Phase 2 forward; return to this queue when sandbox is back.
2. **PTAB v2 snapshot pull** (TIME-CRITICAL — 5 days left): run the already-authored `tools/ptab_v2_snapshot.py` against `developer.uspto.gov` PTAB v2 before decommission, persist to `baselines/ptab_baseline_proceedings.json`. Do this BEFORE Exhibit-21 table build — opportunity closes permanently.
3. **Run offline Stage 1 tests**: `python tools/test_party_resolver.py`. Expected 0 failures. If any fail, fix in place before proceeding.
4. **Build Exhibit-21 table**: run the already-authored `tools/build_exhibit21_table.py --top 25` (minimum viable floor), expand to `--top 100` if throughput allows. Populates `baselines/exhibit21_subsidiary_table.json`.
5. **Seed the cache**: hand-curate 20 cases into `baselines/party_resolution_cache.json` — mix of S&P 500 majors, wholly-owned subs (forcing Exhibit-21 path), individuals, and government agencies (negative coverage).
6. **Stage 2 smoke test**: run `party_resolver.py` on all 20 seed cases, record pass/fail and the confidence distribution into a new PROGRESS_LOG block. If ≥18/20 resolve at confidence ≥0.85, Phase 2 is COMPLETE; if not, triage failures before declaring complete.
7. **Resolve Q-005**: follow EDIS PDF 301 redirect with `-L`; determine whether REST API is viable or UI scrape is the path for Phase 4 ITC scanner.
8. Do NOT touch scanner code (`tools/*_scanner.py`) until Phase 2 is COMPLETE (not partial). F-05.

---

## Tool Health

| Tool | Status | Notes |
|------|--------|-------|
| `tools/party_resolver.py` | ⚠️ CODED, NOT SMOKE-TESTED | Stage 1 offline-clean; Stage 2 unvalidated against live HTTP |
| `tools/test_party_resolver.py` | ⚠️ AUTHORED, NOT YET RUN | Blocked on sandbox; stdlib-only, should run green |
| `tools/build_exhibit21_table.py` | ⚠️ AUTHORED, NOT YET RUN | Session 4; seed CIK list embedded; ready to run when sandbox returns |
| `tools/ptab_v2_snapshot.py` | ⚠️ AUTHORED, NOT YET RUN | Session 4; run FIRST next session before 2026-04-20 decommission |
| `tools/pacer_recap_scanner.py` | NOT BUILT | Phase 3 |
| `tools/itc_337_scanner.py` | NOT BUILT | Phase 4; UA-map per D-015; Q-005 open |
| `tools/ptab_ipr_scanner.py` | NOT BUILT | Phase 4; BLOCKED on Q-004 (PTAB v3 WAF) |
| `tools/delaware_chancery_scanner.py` | NOT BUILT | Phase 4; redesigned per D-016 |
| `tools/sec_enforcement_scanner.py` | NOT BUILT | Phase 3 |
| `tools/doj_ftc_antitrust_scanner.py` | NOT BUILT | Phase 4 |
| `tools/convergence_engine.py` | NOT BUILT | Phase 4 |
| `tools/scorer.py` | NOT BUILT | Phase 4 |
| `baselines/party_resolution_cache.json` | ⚠️ SCHEMA-ONLY | Empty `entries`; 20-seed population pending |
| `baselines/exhibit21_subsidiary_table.json` | ⚠️ SCHEMA-ONLY | Empty `entries`; top-25→top-100 population pending |
| `baselines/executive_lookup.json` | NOT CREATED | Phase 2b or Phase 3 (DEF 14A parse) |
| `baselines/ptab_baseline_proceedings.json` | NOT CREATED | **CRITICAL: 5-day window to pull v2 snapshot before 2026-04-20** |

### Endpoint status (unchanged from Session 2 — no new probes this session)

| Channel / Source | Status as of 2026-04-14 |
|------------------|--------------------------|
| CourtListener RECAP API v4 | ✅ VERIFIED |
| USITC news releases | ✅ VERIFIED (UA-sensitive per D-015) |
| USITC EDIS external UI | ✅ VERIFIED; REST spec ⚠️ Q-005 |
| USPTO PTAB v2 | ⛔ DECOMMISSIONING 2026-04-20 |
| USPTO PTAB v3 | ⚠️ WAF-GATED (Q-004) |
| Delaware CourtConnect | ✅ VERIFIED (no CAPTCHA; D-016) |
| Delaware Chancery opinions | ✅ VERIFIED |
| SEC EDGAR (getcurrent, full-text, litreleases) | ✅ VERIFIED |
| DOJ ATR press releases | ✅ VERIFIED (scrape; no ATR-specific RSS) |
| FTC competition RSS | ✅ VERIFIED |
| OpenFIGI v3 (support) | ✅ RE-VERIFIED |
| data.sec.gov (support) | ✅ RE-VERIFIED |
| Wikidata / SPARQL (party resolution) | ✅ VERIFIED |
| EDGAR Exhibit-21 corpus (party resolution) | ✅ VERIFIED |

---

## Session Lock State

`SESSION_LOCK.md` was re-LOCKED at 2026-04-14T13:30:00Z for Session 4 narrow offline-script prep, then released UNLOCKED on clean exit.

---

## Outstanding Scheduled Tasks

None registered yet. Per D-012, four tasks will be registered in Phase 5 once scanners exist and Phase 4 smoke-tests clean:
- `litigation-operational` — `0 */6 * * *`
- `litigation-maintenance` — `50 */6 * * *`
- `litigation-performance-report` — `30 1 * * *`
- `litigation-deep-dives` — `30 */8 * * *`

SKILL.md source-of-truth copies live under `skills/<task-id>/SKILL.md` (D-013). Authored in Phase 0, unchanged in Sessions 2 and 3, ready for deploy at Phase 5 registration time.
