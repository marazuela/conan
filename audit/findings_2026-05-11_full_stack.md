# Findings — 2026-05-11 (Full-Stack Audit)

## Summary

**Total findings: 47** (P0: 7, P1: 17, P2: 18, P3: 5)

Across 7 layers — scanners, reactor, orchestrator, dashboard/views, skills, observability, infra — using 4 methods (static code, live DB probing, skill-vs-code diff, cron/schedule verification).

**Highest-impact (P0):** today (2026-05-11) the v3 pipeline is in degraded state — `asset_linker_24h_hard_halt` critical flag is open, 4 critical cron jobs were deactivated in response, and Anthropic API credits are exhausted causing 57% of today's orchestrator runs to fail. Compounding this: PR #30's intent to halt deprecated v2 scanners is not enforced (`edgar_filing_monitor` still emitting 485 signals/week despite `status='deprecated'`), and 4 registered fetchers have never run.

## Methodology

- **Phase 1 (static code, 4 parallel agents):** read source in `modal_workers/`, `supabase/functions/`, `orchestrator_runtime/`, `skills/` for anti-patterns, dead code, contract mismatches.
- **Phase 2 (live DB):** 13 queries against the live Supabase project `xvwvwbnxdsjpnealarkh` covering orphans, drift, DLQ depth, sweeper output, scanner liveness, post-PR#30 verification, daily-cap checks.
- **Phase 3 (skill-vs-code):** read `skills/thesis_writer`, `skills/candidate_aging`, `skills/signal_resolver`, `skills/bulk_orchestrator` SKILL.md against committed code + live DB.
- **Phase 4 (cron/schedule):** cross-referenced `cron.job`, `cron.job_run_details`, `public.scanners.scheduled_hour_utc`, `unified_system/config/scanner_registry.json`, and pg_cron migrations.

**NOT covered (gaps):**
- `marazuela/conan-dashboard` repo (separate, not in this worktree).
- Cowork-resident scheduled-task skills (`thesis_writer`, `signal_resolver`, `candidate_aging`) — invisible via MCP per memory `cowork_scheduled_tasks.md`.
- Modal-private code paths.
- `scoring-state.ts` line-by-line stale-stamping mode verification (would need targeted re-read).
- Code-only re-verification of carry-forward findings F-001, F-002, F-202, F-203 from 2026-04-27 — most carry forward; see F-100 series below.

ID space: F-100..F-199 to avoid collision with prior audits' F-001..F-064 and F-2xx range.

---

## Layer: Infra / Today's degraded state (P0)

### F-100  Asset linker hard-halted on 24h cost — critical flag open
- Severity: P0
- Method: live-db
- Evidence: `operator_flags` row, severity=critical, source=orchestrator_cost, kind=`asset_linker_24h_hard_halt`, created 2026-05-11 20:49 UTC.
- Recommendation: investigate cost spike (likely correlated with credit-exhaustion API errors causing retries). Inspect `orchestrator_runs.cost_actual_usd` for today.
- Status: spawn-task

### F-101  Anthropic API credits exhausted — driving cascading orchestrator failures
- Severity: P0
- Method: live-db
- Evidence: `orchestrator_runs` failed today, `error_message='...credit balance is too low...'` (1 direct hit, 2026-05-11 18:35 UTC). Plus 9 runs with `error_message='Ensemble produced 0 successful runs'` (cascading from the credit issue). Plus 2 with read-timeout on `xvwvwbnxdsjpnealarkh.supabase.co`.
- Recommendation: refill credits; add cost/credit precheck in [modal_workers/orchestrator_app.py](modal_workers/orchestrator_app.py) `orchestrator_run_one` so runs fail fast instead of spawning ensemble + premortem before crashing.
- Status: spawn-task

### F-102  4 critical cron jobs deactivated — pipeline running in pass1-only mode
- Severity: P0
- Method: cron-check
- Evidence: `cron.job.active=false` for `v3-orchestrator-drain` (every 5min), `v3-feedback-loop-daily` (daily 02:00 UTC), `v3-asset-linker-pass2` (every 30min), `v3-fact-extractor` (every hour). All four have run history through 2026-05-11 — they were deactivated *today*, likely in response to F-100. Only `v3-asset-linker-pass1`, `v3-pipeline-watchdog`, `scanner-liveness-watchdog` remain active.
- Recommendation: confirm intentional cost throttle and document timeline; consider a coordinated re-enable with cost caps.
- Status: spawn-task

### F-103  5 v3_pipeline_watchdog warns open today — backlog cascading
- Severity: P0
- Method: live-db
- Evidence: open `operator_flags` from `v3_pipeline_watchdog` today: `asset_linker_burn_rate_high`, `drainer_tier2_pending_too_long`, `asset_linker_pass1_backlog`, `fda_assets_no_docs`, `fact_extractor_stalled`. All warn-severity, all created 2026-05-11. Confirms F-102 cascading effect.
- Recommendation: build a `v_open_watchdog_flags_today` filter into dashboard so this cluster is impossible to miss.
- Status: spawn-task

---

## Layer: Scanners (PR #30 enforcement + scheduling)

### F-104  `edgar_filing_monitor` (deprecated) still writing 485 signals/week
- Severity: P0
- Method: live-db
- Evidence: `scanners.status='deprecated'` for `edgar_filing_monitor` (id `58ad0e7a-...`), yet `signals` table shows 485 new rows in last 7d, latest 2026-05-11 14:36 UTC. 54 successful scanner_runs in 7d. PR #30 ([748e451](https://github.com/marazuela/conan/pull/30)) intended to halt v2 emitters; the dispatcher is not honoring `status='deprecated'`.
- Recommendation: locate the dispatch entry (likely [modal_workers/scanner_dispatcher.py](modal_workers/scanner_dispatcher.py) or similar) and add `WHERE status NOT IN ('deprecated','disabled')` guard. Cross-check against memory `scanner_registry_vs_db.md`.
- Status: **FIXED 2026-05-14** — Already closed by PR [#47](https://github.com/marazuela/conan/pull/47) (commit [0e9dbac](https://github.com/marazuela/conan/commit/0e9dbac), merged 2026-05-13 10:22 +0200) which emptied the hardcoded `_DEFAULT_SCANNERS_3H` / `_DEFAULT_SCANNERS_WEEKLY` fallback lists in [modal_workers/app.py:637-638](modal_workers/app.py) used by `_load_cadence_names` on registry-failure. Status gating is enforced two ways: (1) REST filter `status=eq.operational` in `shared/supabase_client.py:load_operational_names_by_cadence` and `load_operational_daily_names_for_hour` (since 2026-04-22 commit [175ded7](https://github.com/marazuela/conan/commit/175ded7)); (2) per-spawn skip in `app.py:716-724` (`_dispatch` — `if status is not None and status != "operational": continue`). Positive-filter form is strictly safer than the audit's suggested `NOT IN ('deprecated','disabled')` — `disabled` isn't a valid state (check constraint: `'operational','planned','deprecated','experimental','paused','shadow','shadow_with_emit'`). No code change in this branch; verification only. Live-DB verification: zero `scanner_runs` and zero `signals` rows from ANY of the 24 deprecated scanners since PR #47 deploy (2026-05-13 ~09:30 UTC). `edgar_filing_monitor` specifically: last activity 2026-05-11 14:36 UTC, none since. Daily deprecated-signal counts before/after the cutover: 2026-05-11=105, 2026-05-12=0, 2026-05-13=0, 2026-05-14=0. Filter is not over-broad — `fda_signal_bridge` (operational, 3h cadence) ran 2026-05-14 08:35 UTC.

### F-105  4 operational scanners never ran in last 7 days
- Severity: P0
- Method: cron-check
- Evidence: `scanners.status='operational'` with `scheduled_hour_utc=13` but 0 runs_7d: `fda_adcomm_pdufa`, `sec_8k_mna`, `edgar_8k_pdufa`, `fed_register_adcom`. All four have `config->>'probe_skip_reason'` indicating they were registered as fetchers with duplicate endpoints. Memory note `fetchers_use_scanner_runs.md` (2026-05-11) says these should be running.
- Recommendation: either (a) wire the cron schedule for fetcher-class scanners or (b) mark them `status='disabled'` and document. Currently they're operational-by-status but dead-by-cron.
- Status: **FIXED 2026-05-14** — investigation showed two of the four were audit miscalibrations + two were genuinely broken; per-scanner outcome:
  - `fda_adcomm_pdufa` — **audit miscalibration.** Healthy fetcher. 130 rows in `catalyst_universe` (source_feed=`openfda_drugsfda`) in 7d, latest 2026-05-13 13:00 UTC. Fetcher class doesn't open `scanner_runs` (memory `fetchers_use_scanner_runs.md`), so cron-check method undercounted activity. No code change.
  - `sec_8k_mna` — **audit row stale.** Live DB shows `status='deprecated'`, not `operational`. Already retired 2026-05-13 as part of v3-only adoption (`modal_workers/app.py` comment at line 581). No action.
  - `edgar_8k_pdufa` — **WIRED.** Fetcher module + Modal `*_once` entry point + `_FETCHERS_AT_HOUR[13]` extension shipped on branch `claude/f-105-fetcher-wiring` (commit [c4f7774](https://github.com/marazuela/conan/commit/c4f7774)) via PR [#66](https://github.com/marazuela/conan/pull/66). Modal deployed 2026-05-14 ~11:45 UTC (conan-v2 app, function `edgar_8k_pdufa_once` visible). Pre-fix observation: 13 rows from a one-shot manual run on 2026-05-11 21:06 (single batch, 4-second window), then zero. First organic 13 UTC tick is 2026-05-14 13:00 UTC — verify with the query below.
  - `fed_register_adcom` — **WIRED.** Same shipment + commit + PR + deploy. Pre-fix observation: zero `event_type='adcom'` rows in `fda_regulatory_events` ever. First organic tick 2026-05-14 13:00 UTC.

  Verification query (run after 2026-05-14 13:00 UTC):
  ```sql
  SELECT extensions->>'source_feed' AS source_feed, event_type, COUNT(*) AS rows,
         MIN(created_at), MAX(created_at)
  FROM public.fda_regulatory_events
  WHERE created_at >= '2026-05-14 13:00:00+00'
    AND extensions->>'source_feed' IN ('edgar_8k_pdufa', 'fed_register_adcom')
  GROUP BY 1, 2;
  ```

  Notes:
  - The 5 lifted files came from uncommitted WIP in Pedro's main repo working tree (`claude/orchestrator-v3-phase-0` branch) — Pedro confirmed via AskUserQuestion to adopt the WIP rather than write a parallel version.
  - `supabase db push` was NOT run for this PR due to pre-existing repo-wide migration drift (memory `supabase_migrations_drift.md` — 121 remote migrations lack local `.sql` files). The migration file is committed for parity, but the registration rows it inserts are already present in live DB (applied via MCP `apply_migration` during Pedro's earlier WIP exploration). Follow-up cleanup: `supabase migration repair --status applied 20260521000000` registers our migration's filename in `schema_migrations` without touching the broader drift.
  - F-104 in flight in parallel; no coordination needed — this PR doesn't change scanner status or touch the dispatcher status filter.
  - Cost-side caveat: each new `fda_regulatory_events` INSERT triggers `enqueue_fda_agent_reviews_on_event_insert_tg` and fans into 3 `fda_agent_reviews` rows. Today's degraded state (F-100 asset_linker hard-halt, F-101 exhausted credits, F-102 deactivated crons) means the new specialist-review demand may pile up against already-throttled drainers. Pedro accepted this trade-off at the deploy gate.

### F-106  Scanner clock drift — actual vs scheduled hour mismatch
- Severity: P2
- Method: cron-check
- Evidence: `openfda_corpus_ingest`: scheduled_h=6, avg_actual_h=12.7. `fda_pdufa_pipeline`: scheduled_h=13, avg_actual_h=15.4. `pre_phase3_readout_scanner`: scheduled_h=13, avg_actual_h=14.6. Drift suggests the cron caller doesn't actually use `scheduled_hour_utc` — it may be informational only.
- Recommendation: either remove `scheduled_hour_utc` as misleading, or wire it into the dispatcher.
- Status: spawn-task

### F-107  FDA scanner HTTP 4xx/5xx silently treated as success
- Severity: P1
- Method: static
- Evidence: [modal_workers/scanners/fda_pdufa_pipeline.py:957-963](modal_workers/scanners/fda_pdufa_pipeline.py) — `requests.get()` checks `if r.status_code != 200` but does NOT call `raise_for_status()`. Lines 815 and 907 in same file DO call it correctly.
- Status: spawn-task

### F-108  Delaware Chancery scanner: 4xx indistinguishable from 5xx
- Severity: P1
- Method: static
- Evidence: [modal_workers/scanners/delaware_chancery_scanner.py:445-451](modal_workers/scanners/delaware_chancery_scanner.py) — `_fetch_opinions()` returns `None` for any non-200; 401/500 indistinguishable from 404. (Note: scanner is also `status='deprecated'` so impact is low — this is P1 not P0 because of dual status.)
- Status: spawn-task

### F-109  CourtListener scanner: GET without status check in `_fetch_nos`
- Severity: P1
- Method: static
- Evidence: [modal_workers/scanners/courtlistener_scanner.py:248-250](modal_workers/scanners/courtlistener_scanner.py) — no `raise_for_status()`. 429 returns `{"results":[]}` which scanner treats as "no results".
- Status: spawn-task

### F-110  Takeover scanner: HTTP failures fall through to entity_resolution=None
- Severity: P1
- Method: static
- Evidence: [modal_workers/scanners/takeover_candidate_scanner.py:283-298](modal_workers/scanners/takeover_candidate_scanner.py) — `_check_post_edge()` and `_get_ticker_exchange()` swallow exceptions, return None/False on any non-200. (Scanner deprecated; downgraded.)
- Status: spawn-task

### F-111  EDGAR EFTS pagination has no truncation guard (related to F-122 prior)
- Severity: P2
- Method: static
- Evidence: [modal_workers/ingestion/edgar_ingest.py:88-96](modal_workers/ingestion/edgar_ingest.py) — `efts_search()` returns a flat list; if API truncates at 50, scanner silently stops. Contrast `clinicaltrials_ingest.py:142-144` which checks `nextPageToken`.
- Status: spawn-task

### F-112  ClinicalTrials.gov pagination: empty-string token causes infinite-but-broken loop
- Severity: P2
- Method: static
- Evidence: [modal_workers/ingestion/clinicaltrials_ingest.py:142-144](modal_workers/ingestion/clinicaltrials_ingest.py) — exits only when `nextPageToken` is falsy. Empty string `""` from malformed response triggers next iteration with `pageToken=""` which breaks downstream.
- Status: spawn-task

### F-113  Federal Register: first 5xx aborts remaining pages
- Severity: P2
- Method: static
- Evidence: [modal_workers/ingestion/federal_register_ingest.py:87-101](modal_workers/ingestion/federal_register_ingest.py) — loop breaks on first `FederalRegisterError`, suppressing pages 2-N. Compare openfda_ingest which has retry.
- Status: spawn-task

### F-114  `openfda_corpus_ingest` mode env var unvalidated
- Severity: P3
- Method: static
- Evidence: [modal_workers/scanners/openfda_corpus_ingest.py:46,62](modal_workers/scanners/openfda_corpus_ingest.py) — `OPENFDA_INGEST_MODE` env var with no validation; unknown values default silently to "shallow". Combine with last_run_status='error' today and this is worth tightening.
- Status: spawn-task

---

## Layer: Reactor + edge functions

### F-115  `RESEND_API_KEY ?? ""` defeats the empty-check
- Severity: P1
- Method: static
- Evidence: [supabase/functions/fanout/index.ts:91](supabase/functions/fanout/index.ts) — `Deno.env.get("RESEND_API_KEY") ?? ""` — the `??` only fires on `null`/`undefined`, so an unset secret becomes `""`. Later guards `if (!RESEND_API_KEY)` correctly treat empty as falsy, but the defaulting pattern is fragile and mirrors F-202.
- Status: spawn-task

### F-116  `await r.json().catch(() => ({}))` swallows Resend 5xx body
- Severity: P2
- Method: static
- Evidence: [supabase/functions/fanout/index.ts:476,699,924](supabase/functions/fanout/index.ts) — when Resend returns non-JSON 5xx HTML, the catch wipes it. The error log shows `body={}` instead of the actual reason.
- Recommendation: log `r.status` and raw text length before the `.json()` attempt.
- Status: spawn-task

### F-117  `clearDisplacedWinners` updates not transactional
- Severity: P2
- Method: static
- Evidence: [supabase/functions/reactor/index.ts:827-850](supabase/functions/reactor/index.ts) — serial UPDATE loop without wrapping transaction. Two concurrent reactor invocations on the same `convergence_key` can race and one's clear can be re-stamped by the other. This is reactor stale-stamping mode #4 ("clearDisplacedWinners race") per memory.
- Recommendation: single multi-row UPDATE with `WHERE convergence_key=X AND convergence_bonus>0 AND signal_id <> :winner` or wrap in `BEGIN;…COMMIT;`.
- Status: spawn-task

### F-118  `fetchWithRetry` does not retry 4xx (including transient 409)
- Severity: P1
- Method: static
- Evidence: [supabase/functions/reactor/fetch-retry.ts](supabase/functions/reactor/fetch-retry.ts) retries on 5xx/408/429 but passes through all 4xx. A transient 409 Conflict on `rubric-apply-caps` deserves a retry.
- Status: spawn-task

### F-119  `flagHeuristicMissingScoringMeta` call is not awaited
- Severity: P1
- Method: static
- Evidence: [supabase/functions/reactor/index.ts:354](supabase/functions/reactor/index.ts) — fire-and-forget. If reactor crashes after the call but before the operator_flag INSERT lands, the flag is lost. (Carries forward from prior F-214.)
- Status: spawn-task

### F-120  `convergence.ts windowDays` uses `profiles.includes("litigation")` substring match
- Severity: P3
- Method: static
- Evidence: [supabase/functions/_shared/convergence.ts:110](supabase/functions/_shared/convergence.ts) — substring match. Fragile if a future profile name contains "litigation".
- Status: spawn-task

### F-121  Email-gating: routine_declined='true' is NOT filtered by fanout
- Severity: P0
- Method: skill-diff
- Evidence: skill `skills/thesis_writer/SKILL.md:659` requires `fanout` to skip events where `payload->>'routine_declined'='true'`. Code at [supabase/functions/fanout/index.ts:165-171](supabase/functions/fanout/index.ts) routes `event_type IN ('created','thesis_drafted_by_claude')` directly to `dispatchPreEdgePromotion` with NO `routine_declined` check. **Flagged candidates will currently fire emails** in violation of skill spec.
- Recommendation: add `if (payload.routine_declined === 'true') return { skipped: 'routine_declined' };` at dispatch_pre_edge_promotion entry.
- Status: **FIXED 2026-05-14** — commit [7e7da3e](https://github.com/marazuela/conan/commit/7e7da3e) on branch `claude/dreamy-black-f823f4`. Guard placed at the `candidate_events` branch entry (line 165-180) before the `event_type` switch — covers both the `created`/`thesis_drafted_by_claude` path AND the feature-flagged killed/delivered path with one check. Permissive type check (`rd === true || rd === "true"`) matches both the skill's JSON-boolean write and the parallel SQL string-form filter on `candidates.extensions->>'routine_declined'`. Deployed as fanout v12 (`verify_jwt=false` preserved). Smoke tests: boolean `true` → `{"skipped":"routine_declined"}` 200; string `"true"` → same; control (absent) → falls through to dispatch (500 on bogus candidate_id as expected). Pre-fix damage: 10 emails to 5 distinct flagged candidates between 2026-05-07 and 2026-05-11.

---

## Layer: v3 orchestrator + Modal

### F-122  Stage 10 INSERT lacks ON CONFLICT — re-run double-writes
- Severity: P1
- Method: static
- Evidence: [orchestrator_runtime/runtime.py:946-962,976-987](orchestrator_runtime/runtime.py) — `hypothesis_enumeration` and `premortem_assessments` INSERTs have no `ON CONFLICT DO NOTHING`/`UPDATE`. Comment on line 1097 claims idempotency. If `orchestrator_run_one` is retried (e.g., after timeout from F-101), the same hypotheses get inserted twice.
- Status: spawn-task

### F-123  Cost field name mismatch: orchestrator_runs.cost_actual_usd vs convergence_assessments.cost_usd
- Severity: P2
- Method: static
- Evidence: [modal_workers/orchestrator_app.py:405-423](modal_workers/orchestrator_app.py) writes `cost_actual_usd` to orchestrator_runs while [orchestrator_runtime/runtime.py:898](orchestrator_runtime/runtime.py) writes `cost_usd` to convergence_assessments. Downstream dashboards and the new `v_cost_24h_by_worker` view need to know which to read.
- Status: spawn-task

### F-124  `fact_extractor_run` returns only `{return_code}` — partial-state loss
- Severity: P2
- Method: static
- Evidence: [modal_workers/orchestrator_app.py:227-239](modal_workers/orchestrator_app.py) — `extractor_main(argv)` failure mid-document loses per-document success/fail accounting. Combined with `fact_extractor_stalled` watchdog flag (F-103), worth instrumenting.
- Status: spawn-task

### F-125  `all_falsified` conviction cap: equality off-by-one risk
- Severity: P2
- Method: static
- Evidence: [orchestrator_runtime/runtime.py:1661-1674](orchestrator_runtime/runtime.py) — `min(raw_conv, 30.0)`; when `raw_conv == 30.0`, `capped < raw_conv` is false so the cap-applied flag never fires. Likely cosmetic but the `ctx["pre_premortem_conviction"]` semantics depends on it.
- Status: spawn-task

### F-126  Stage 2 → Stage 6 ensemble: `parsed_json` not validated before Stage 2
- Severity: P2
- Method: static
- Evidence: [orchestrator_runtime/runtime.py:1556](orchestrator_runtime/runtime.py) — if Stage 6 ensemble in streaming mode exits early (budget exhausted), `parsed_json` may be stale or missing; Stage 2 ingests it without a None check.
- Status: spawn-task

### F-127  `--skip-stage-7-constitutional-deterministic-only` leaves citation_resolution_failures empty
- Severity: P2
- Method: static
- Evidence: [orchestrator_runtime/runtime.py:1784](orchestrator_runtime/runtime.py) — Stage 7 skip flag has no warning that Stage 10's citation-resolution writeback expects Stage 7 output. Downstream fact-linker assertions silently get empty failures.
- Status: spawn-task

### F-128  `check_24h_thresholds` exception is silently swallowed
- Severity: P3
- Method: static
- Evidence: [modal_workers/orchestrator_app.py:458-462](modal_workers/orchestrator_app.py) — bare `except Exception: pass` after `check_24h_thresholds`. If Supabase is briefly unavailable, the run reports success without the threshold check; no flag is raised. Combined with F-101 read-timeout error, this is the kind of swallow that hides real degradation.
- Status: spawn-task

### F-129  `ic_memo_runner.load_ic_memo_context` doesn't validate all 4 specialists present
- Severity: P2
- Method: static
- Evidence: [orchestrator_runtime/ic_memo_runner.py:65-80](orchestrator_runtime/ic_memo_runner.py) — silently inserts None for missing specialist rows (literature, competitive, regulatory_history, options_microstructure). Memo runs with incomplete context.
- Status: spawn-task

---

## Layer: Skills (skill-vs-code drift)

### F-130  Stage A (mechanical) candidate_aging automation has no committed runner
- Severity: P1
- Method: skill-diff
- Evidence: skill `skills/candidate_aging/SKILL.md` §3 defines Stage A rules (watch→active, aged-out, stale-active demote at 60d, recent-elapsed flag). No SQL trigger, no Modal function, no Python skill runner found in code. Per memory `cowork_scheduled_tasks.md`, the runner lives in Pedro's Cowork session — invisible to MCP and to this audit. **Confirmation gap**, not necessarily broken.
- Status: open (gap documented)

### F-131  `routine_declined` gate on watch→active promotion: code-side enforcement absent
- Severity: P1
- Method: skill-diff
- Evidence: skill `skills/candidate_aging/SKILL.md:56` mandates `extensions->>'routine_declined' IS DISTINCT FROM 'true'` for promotion. No SQL trigger or RPC enforces this. If the Cowork session is misconfigured or skipped, candidates flagged `routine_declined=true` can still promote.
- Status: spawn-task

### F-132  Short-positioning sub-quota (5/day) skill-only
- Severity: P1
- Method: skill-diff
- Evidence: skill `skills/thesis_writer/SKILL.md:60-70,105-113` defines short ranking + `profile_deferred_short_limit` gate. No DB CHECK, no scanner config `daily_promotion_limit` seeded, no enforcement found. Note: per memory `short_positioning_calibration.md` (2026-04-27) shorts are scored ~17pts higher; combined with no quota, shorts could dominate.
- Status: spawn-task

### F-133  Challenger verdict routing (`challenge`→retry, `kill`→DLQ) skill-only
- Severity: P1
- Method: skill-diff
- Evidence: skill `skills/thesis_writer/SKILL.md` §8d–8f, `skills/candidate_aging/SKILL.md` §5.5. No DB or Modal Python code routes these verdicts. Memory migration 20260423010000 (thesis_challenger routine) exists but doesn't enforce the verdict-routing contract.
- Status: open (gap documented)

### F-134  `attempt_count_exhausted` gate_reason has historical row but no committed code
- Severity: P3
- Method: live-db + skill-diff
- Evidence: `SELECT count(*) FROM thesis_jobs WHERE 'attempt_count_exhausted' = ANY(gate_reasons)` returns 1 historical row. Per memory `dlq_token_audit_gap.md`, this token is written by some skill but absent from committed code. Suggests JGoror Cowork-session-resident skill drift.
- Status: informational

### F-135  No code-side enforcement of the 15/day `thesis_daily_cap` for `thesis_jobs` (only `orchestrator_runs`)
- Severity: P2
- Method: skill-diff
- Evidence: `internal_config.thesis_daily_cap='15'` and function `orchestrator_runs_daily_cap_check` enforces it on `orchestrator_runs WHERE status IN ('pending','running','retrying')`. But the *intent* per skill spec and PR #30 description is to cap *thesis drafting* — which goes through `thesis_jobs.status='promoted'`. Live data shows `promoted` daily counts (4 today, peak 4) well under 15, but the absence of a parallel cap on `thesis_jobs` means a future change could bypass.
- Status: spawn-task

---

## Layer: Observability + views

### F-136  `orphan_convergence_sweeper` swallows all exceptions silently (carry-forward)
- Severity: P1
- Method: static
- Evidence: [modal_workers/observability.py](modal_workers/observability.py) ~line 660 — `except Exception` with `noqa: BLE001` swallows sweeper errors. Per memory `dispatch_observability_silent_swallow.md`. Today's data shows 45 orphans created (P3 spike), suggesting the sweeper is overwhelmed or partially failing.
- Status: spawn-task (carry-forward)

### F-137  Orphan-signal rate spiked to 45/day today (vs <20/day baseline)
- Severity: P1
- Method: live-db
- Evidence: per-day `signals.score IS NOT NULL AND band_with_bonus IS NULL` count: 2026-05-11=45, 05-10=2, 05-09=2, 05-08=19, 05-07=7, 05-06=1, 05-05=3. Today's spike correlates with F-101 API failures.
- Recommendation: trace whether reactor is the source (fails mid-classifyGroup) or downstream sweeper is the source (fails to backfill).
- Status: spawn-task

### F-138  `fda_regulatory_events` table frozen at 2026-05-04 (7 days stale)
- Severity: P1
- Method: live-db
- Evidence: max(created_at)=max(updated_at)=2026-05-04 13:41 UTC. 35 rows total. Memory `catalyst_universe_vs_fda_regulatory_events.md` says this is operator-script-fed (not scanner-fed) and that catalyst_universe (1791 rows, max 2026-05-11) is the live counterpart. The v3 orchestrator routes via `fda_regulatory_events` per `orchestrator-enqueue.ts`, so this drift may starve the v3 pipeline of new FDA events.
- Recommendation: build the bridge from `catalyst_universe` → `fda_regulatory_events`, or change orchestrator-enqueue to source from `catalyst_universe`.
- Status: spawn-task

### F-139  Dashboard views applied despite commit message stating "drafted, not applied"
- Severity: P3
- Method: live-db
- Evidence: commit `e6df2dd` message says Phase A v3 dashboard views are drafted-not-applied, but all five views exist and return rows: `v_thesis_inbox` (183), `v_open_operator_flags` (6), `v_latest_assessments_by_asset` (89), `v_assessment_stage_chain` (98), `v_cost_24h_by_worker` (exists).
- Recommendation: amend commit history or PR description; reduces future confusion.
- Status: informational

### F-140  `precision_auditor` and `timing_auditor` lack per-call try/except in observability dispatch
- Severity: P2
- Method: static
- Evidence: [modal_workers/observability.py:1525,1872](modal_workers/observability.py) — these two sweepers run only on Sundays (`weekday()==6`). Other sweepers in the dispatch loop have individual try/except; these two don't. A Sunday-only crash takes down the entire dispatch silently.
- Status: spawn-task

### F-141  `litigation_baselines_refresh` missing-cache flagged as `severity='info'`
- Severity: P1
- Method: static
- Evidence: [modal_workers/observability.py:687-695](modal_workers/observability.py) — flag `kind='party_cache_missing'` with `severity='info'`. Cache is essential for party-resolution accuracy.
- Recommendation: bump to `severity='warn'` minimum.
- Status: spawn-task

### F-142  `thesis_jobs_sla_sweeper` 3-attempt cap bypassable under concurrent sweeps
- Severity: P2
- Method: static
- Evidence: [modal_workers/observability.py:988-1015](modal_workers/observability.py) — `if next_attempt >= 3 then dlq else reset`. If two dispatch invocations overlap (no FOR UPDATE SKIP LOCKED), both can read attempt_count=2 and re-reset.
- Status: spawn-task

### F-143  Orchestrator pre-mortem result handling: `pre_mortem_verdict=NULL` when Stage 2 emits no hypotheses
- Severity: P2
- Method: static
- Evidence: [orchestrator_runtime/runtime.py:1676](orchestrator_runtime/runtime.py) — when Stage 2 emits zero hypotheses, log warns "skipping Stage 3"; `premortem_result` stays None; Stage 10 inserts convergence_assessments with `pre_mortem_verdict=NULL`. Schema doesn't NOT NULL it, but downstream view `v_latest_assessments_by_asset` may treat NULL inconsistently.
- Status: spawn-task

### F-144  Dashboard view `v_thesis_inbox` reads only `public.signals`, hides `archive_v2.signals`
- Severity: P2
- Method: static + live-db
- Evidence: [supabase/migrations/20260522000000_v3_dashboard_views.sql:271](supabase/migrations/20260522000000_v3_dashboard_views.sql). Phase 1 archived v2 to `archive_v2.signals` (2395 rows). View intentionally scoped to v3, but lineage gap means an operator investigating an old alert can't trace it via the dashboard.
- Recommendation: document the v3-only scope at the top of the migration; consider a parallel `v_thesis_inbox_archive` for audit lookups.
- Status: spawn-task

---

## Layer: Carry-forward open findings from prior audits

### F-145  F-001, F-002 (silent pagination loss in scanners) — both scanners deprecated, risk mitigated
- Severity: P3
- Method: live-db
- Evidence: `congressional_trading` and `asx_scanner` both `status='deprecated'`, 0 runs in last 7d. Carry-forward findings F-001 and F-002 from 2026-04-27 audit are now low-impact since the scanners are off. But the *pattern* (silent break on empty page) still exists in active code per F-111, F-112, F-113.
- Status: informational (superseded by F-111/F-112/F-113)

### F-146  F-202/F-203 (reactor resilience, hardcoded URLs) — partially fixed
- Severity: P2
- Method: live-db + static
- Evidence: `internal_config` has 7 `modal_url_*` rows (good — externalized). [supabase/functions/reactor/fetch-retry.ts](supabase/functions/reactor/fetch-retry.ts) exists (good — retry logic). But F-118 (4xx not retried) and F-115 (RESEND fallback) carry the same anti-pattern forward into fanout.
- Status: informational (superseded by F-115/F-118)

### F-147  F-204 (score_signal fallback to activist_governance vs rescore raises) — not re-verified this pass
- Severity: P1
- Method: gap
- Evidence: agent A1 did not surface F-204 specifically. Worth a targeted Phase 1 follow-up on `modal_workers/shared/rubric_engine.py` or wherever `score_signal` lives.
- Status: open (gap documented)

---

## Layer: Misc / cleanup

### F-148  `fda_signal_bridge` operational but emits 0 signals (config.mode='shadow')
- Severity: P3
- Method: live-db
- Evidence: `scanners.config->>'mode'='shadow'` with comment "Phase 3 shadow run. Flip to shadow_with_emit then operational at cutover." 35 successful runs in 7d, 0 signals emitted. Expected behavior — flagged for visibility.
- Status: informational

### F-149  `internal_config.compute_secret` stored in plaintext
- Severity: P2
- Method: live-db
- Evidence: `key='compute_secret'`, value visible as plaintext. Per memory `compute_auth_setup.md` this is expected, but worth verifying RLS or service-role-only access on `internal_config`.
- Recommendation: confirm `internal_config` row-level security blocks anon + authenticated reads.
- Status: spawn-task

### F-150  TODO/FIXME concentrated in 4 files (per Phase 1 sweep)
- Severity: P3
- Method: static
- Evidence: `delaware_chancery_scanner.py`, `sec_8k_mna.py`, `curate_crl_from_edgar.py`, `test_candidate_gate.py` — 1 TODO each. Mostly low-impact but worth a cleanup pass.
- Status: informational

### F-151  Edge function catch blocks stringify PostgrestErrors as `[object Object]`
- Severity: P2 → upgraded to **P1 in retrospect** after live DB inspection (see Evidence). Fix deployed 2026-05-14, all three edge functions.
- Method: live (smoke test 2026-05-14 while verifying F-121 fanout fix; live DB inspection of `failed_reactor_events` confirmed real prod impact)
- Evidence: `supabase/functions/fanout/index.ts:191`, `supabase/functions/reactor/index.ts:199` + `:260`, `supabase/functions/scanner-health/index.ts:122` — all used the idiom `err instanceof Error ? err.message : String(err)`. Supabase JS client throws PostgrestError-shaped objects (`{message, details, hint, code}`), which are NOT `instanceof Error`, so the fallback path runs `String(err)` → literal `"[object Object]"`. Verified live: bogus-candidate POST to fanout returned `HTTP 500 {"error":"[object Object]"}`. Worse: reactor stamps the same value into `failed_reactor_events.error_message`. **Production impact:** 15 of 15 most-recent DLQ rows (2026-05-14 11:40:19–11:40:36 UTC, all `[asset_documents]` path, all `signal_id=NULL`) lost ALL forensic detail — an asset_linker burst threw PostgrestErrors and the operator now has no way to know what failed. Likely correlates with the asset_linker hard-halt (F-100) cascade.
- Fix: shared `_shared/errors.ts` helper (`formatError` + `formatErrorForDlq`) extracts `{message, code, details, hint}` from object-shaped throws. PR [#65](https://github.com/marazuela/conan/pull/65) merged 2026-05-14; deployed same day (reactor v18→v19, fanout v12→v13, scanner-health v6→v7). 6 unit tests + 58/58 full suite passing.
- Status: **FIXED 2026-05-14**. Verification gap: 0 post-deploy DLQ rows yet (asset_linker still hard-halted), so end-to-end format verification pending organic traffic OR a synthetic probe (requires WEBHOOK_SECRET).

### F-152  Edge function `WEBHOOK_SECRET` empty-fallback bypassed both fanout and reactor auth gates
- Severity: **P0** — unauthenticated callers could trigger fanout email dispatch (Resend) and reactor DB-write paths from the open internet for ~3 weeks.
- Method: live (smoke test 2026-05-14 while verifying F-121 fanout fix; verified gate bypass on prod `xvwvwbnxdsjpnealarkh`)
- Evidence: `supabase/functions/fanout/index.ts:90,111-116` and `supabase/functions/reactor/index.ts:144,165-170` (pre-fix) — both sourced the gate secret via `Deno.env.get("WEBHOOK_SECRET") ?? ""` and wrapped the timing-safe compare in `if (WEBHOOK_SECRET)`. The project's edge-function `WEBHOOK_SECRET` env var was **never set** (vault row `webhook_secret` length=43 was set 2026-04-20 and sent correctly by all four Postgres webhook wrappers — `call_fanout`, `call_fanout_assessment`, `call_reactor`, `call_reactor_assetdoc` — but the receiver-side env was missing). Result: `WEBHOOK_SECRET=""` → falsy → entire conditional skipped → no auth check. Confirmed live with `curl -X POST .../functions/v1/fanout` (no headers) returning `HTTP 200 {"skipped":"unsupported table"}`; same on reactor.
- Blast radius: (a) fanout entry points B/C/D triggerable with attacker-supplied payload → Resend emails to subscribed operators with attacker-controlled body, plus DB writes to `alert_deliveries` and storage writes under `reports/promotions/`, `reports/state-changes/`, `reports/assessments/`; (b) reactor signals + asset_documents inserts triggerable → spurious convergence stamping, alerts rows, thesis_jobs enqueue.
- Fix: two halves. **Config**: set project env var `WEBHOOK_SECRET` = vault `webhook_secret` value (done 2026-05-14 via `supabase secrets set --env-file` with digest verification — sha256 match confirmed). **Code**: reactor sweeper path also needed a service-role Bearer bypass (sweeper at `modal_workers/observability.py:574` sends `Authorization: Bearer <service_role>` per `reactor_deploy_no_verify_jwt.md`). Added in reactor v21 — `if (WEBHOOK_SECRET) { accept either x-supabase-webhook-secret OR Bearer matching SERVICE_KEY }`. PR [#68](https://github.com/marazuela/conan/pull/68). Fanout needed no code change.
- Verification 2026-05-14 post-fix: fanout + reactor return 401 without auth header; both return 200 with `x-supabase-webhook-secret: <vault value>` header. Postgres-webhook traffic continues to flow (live test of all 4 wrappers — no DLQ growth, no auth_failures_recent in `failed_reactor_events`).
- Known follow-up: outer `if (WEBHOOK_SECRET)` conditional preserved in reactor v21 only to enable the deploy-before-env-set transition; once env-set is stable in prod, remove the conditional so gate becomes mandatory. Also: Modal sweeper bypass currently broken — edge-function runtime's `SUPABASE_SERVICE_ROLE_KEY` is the new `sb_secret_…` format but Modal sweeper sends the legacy JWT (cleanest fix: set `WEBHOOK_SECRET` in Modal `supabase-secrets` so sweeper passes via the webhook path — being applied 2026-05-14 by operator).
- Status: **MITIGATED 2026-05-14**. Open: (1) drop transitional conditional once env-set is stable; (2) Modal-side `WEBHOOK_SECRET` propagation; (3) harden `Deno.env.get(...) ?? ""` anti-pattern across remaining functions — `RESEND_API_KEY` in fanout is the most prominent surviving instance (silent no-op email dispatch if the key is unset; not a security gap but a similar empty-fallback footgun).

### F-153  `storage-uploader` edge function has zero authentication, writes to any bucket+path via service-role key
- Severity: **P0** — more severe posture than F-152; this function never had any auth, not even a broken one.
- Method: live (`mcp__supabase__get_edge_function storage-uploader` source review while triaging F-152; verified function is ACTIVE v2 on prod with `verify_jwt=false`)
- Evidence: `supabase/functions/storage-uploader/index.ts` (full source — function is NOT in this repo's worktree, only deployed; pulled via MCP) accepts `{bucket, path, content, contentType?}` as JSON body and writes to `${SUPABASE_URL}/storage/v1/object/${bucket}/${path}` using `Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}` + `x-upsert: true`. No `WEBHOOK_SECRET` check, no JWT verification, no rate limit, no path-prefix allowlist. Function is reachable at `https://xvwvwbnxdsjpnealarkh.supabase.co/functions/v1/storage-uploader`.
- Blast radius: attacker can (a) write arbitrary content to any storage bucket in the project (including the `reports` bucket where v2 alert HTMLs render; could plant a phishing-style HTML at a path the dashboard renders), (b) overwrite existing alert HTMLs (`upsert:true`) with attacker-controlled content, (c) fill quota with arbitrary garbage to inflate storage bill or break legitimate writes.
- Unknown: callers. The function is not referenced in this worktree's code (`grep -r storage-uploader supabase/ modal_workers/ unified_system/` returns the function source only, no callers). Likely called from Cowork-resident skills (per memory `cowork_scheduled_tasks.md`, several skills live outside the conan repo). Before adding auth, identify callers so we know which credential to provision.
- Suggested fix: mirror reactor's auth gate — require `x-supabase-webhook-secret` matching project env var, OR `Authorization: Bearer <service_role>` for trusted Modal/Cowork callers, OR (least permissive) `Authorization: Bearer <verified Supabase user JWT>` if dashboard-only. Also add a path-prefix allowlist (`reports/`, etc.) so even authenticated callers can't write outside expected zones.
- Status: **OPEN — P0**. Awaiting caller inventory before fix design. Untouched in this audit cycle's mitigations.

---

## Spawn-task drafts (P0 prioritized)

Each P0 finding above is ready to be spun out as a separate session. The shortlist:

1. **F-100** — Investigate today's asset_linker hard-halt cost spike.
2. **F-101** — Refill Anthropic credits + add fast-fail cost precheck in `orchestrator_run_one`.
3. **F-102** — Coordinated re-enable of `v3-orchestrator-drain`, `v3-feedback-loop-daily`, `v3-asset-linker-pass2`, `v3-fact-extractor` with cost caps in place.
4. **F-103** — Build `v_open_watchdog_flags_today` and surface in dashboard.
5. **F-104** — Add `status NOT IN ('deprecated','disabled')` guard to scanner dispatcher.
6. **F-105** — Schedule or disable the 4 dead fetchers (`fda_adcomm_pdufa`, `sec_8k_mna`, `edgar_8k_pdufa`, `fed_register_adcom`).
7. ~~**F-121** — Add `routine_declined='true'` filter in `fanout/dispatchPreEdgePromotion`.~~ ✓ FIXED 2026-05-14 (commit 7e7da3e, fanout v12)

## Verification ledger

- ✓ Findings doc created at `audit/findings_2026-05-11_full_stack.md` with 47 findings.
- ✓ Every layer covered (scanners, reactor, orchestrator, dashboard/views, skills, observability, infra).
- ✓ All 4 methods produced findings (static, live-db, skill-diff, cron-check).
- ✓ Re-verification of carry-forward findings: F-001/F-002 (superseded by F-111/F-112/F-113), F-202/F-203 (superseded by F-115/F-118), F-204 (gap noted in F-147).
- ✓ Prior 05-11 audit P0s (pre_phase3, openfda_corpus_ingest) re-checked: `pre_phase3_readout_scanner` is running today (13 runs/7d, status=partial), `openfda_corpus_ingest` is running but at wrong hour and erroring (3 runs/7d, 1 error). Both still warrant attention.
- ✓ Memory file `audit_2026-05-11_full_stack.md` added (separate write).
