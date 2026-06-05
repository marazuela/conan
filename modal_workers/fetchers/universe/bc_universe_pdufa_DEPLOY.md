# bc_universe_pdufa — deploy & cron-wiring runbook (Phase 0, THE GATE)

> **Status:** CODE-ONLY in the build tree. **Nothing here has been executed.** No
> `modal deploy`, no `INSERT into public.scanners`, no commit. This runbook is the
> exact, ordered procedure for a human to ship the universe-build cron once Phase 0
> passes the GO/NO-GO and Pedro approves. Read landmine §0 first — the ordering is
> load-bearing.

## 0. The one ordering landmine (do not invert)

`dispatch_release_times` (modal_workers/app.py) resolves each daily scanner to a
spawn target via `getattr(app_module, f"{name}_once")`. A `public.scanners` row
named `bc_universe_pdufa` therefore REQUIRES a deployed `bc_universe_pdufa_once`
Modal function. If the row lands **before** the deploy, the dispatcher tick that
picks it up will raise "function not found" for that scanner (logged in the
dispatch envelope `errors[]`) — a self-inflicted dispatcher error.

**Therefore the order is strictly: (1) deploy the app, THEN (2) INSERT the row.**
To pause/disable later, `UPDATE public.scanners SET status='deprecated'` (the DB
row is authoritative, not scanner_registry.json — memory `scanner_registry_vs_db`).

## 1. Preconditions

- Phase 0 GO verdict recorded (≥15 in-window NDA/BLA, ≥12 tradeable, date-exact
  ≥0.80, FP ≤0.15 on the truth set) via `modal_workers/scripts/bc_phase0_benchmark.py`.
- Modal `scanner-secrets` carries **`SEC_USER_AGENT`**, **`POLYGON_API_KEY`**, and
  **`OPENFDA_API_KEY`** (the last enables Drugs@FDA appno recovery + lifts the
  openFDA daily cap to 120k). `supabase-secrets` carries the service-role key the
  bc_* writes use.
- The four bc_ tables exist and the live CHECK constraints are unchanged
  (verified 2026-06-04): `bc_pipeline_runs.status ∈ {running,succeeded,failed,partial}`,
  `bc_application_features.feature_quality ∈ {standard,low,built_at_install}`,
  `appl_type ∈ {NDA,BLA,sNDA,sBLA}`, `review_priority ∈ {STANDARD,PRIORITY}` (nullable).

## 2. Step 1 — deploy the app (NOT done here)

The `bc_universe_pdufa_once` function is already wired in `modal_workers/app.py`
(image, `secrets=[scanner_secrets, supabase_secrets]`, `timeout=1200`). Deploy per
the canonical topology (memory `orchestrator_deploy_topology` — deploy ONLY from
the approved worktree, gated on `HEAD == origin/main`):

```bash
# from the deploy-authorized worktree, AFTER this code is merged to main:
modal deploy modal_workers/app.py        # ships bc_universe_pdufa_once
```

Smoke-test the function in isolation before registering the cron (this WRITES to
bc_* — it is the first real --apply; run it deliberately, once):

```bash
modal run modal_workers/app.py::bc_universe_pdufa_once
# expect: a bc_pipeline_runs row (status succeeded|partial) + bc_application_features
# rows for the in-window names; a second run same day is an idempotent no-op.
```

## 3. Step 2 — register the cron (the INSERT — DO NOT RUN before step 1)

`scheduled_hour_utc` **MUST be one of {6, 8, 13, 17, 21}** — the only hours
`dispatch_release_times` ticks (`modal.Cron("0 6,8,13,17,21 * * *")`). Any other
value (e.g. 11) means the row is registered but **never fires**. We use **13 UTC**
(US pre-open, ~09:00 ET): it co-locates with the existing FDA 8-K fetchers
(`edgar_8k_pdufa`, `fda_adcomm_pdufa`) and catches the prior US session's
after-close PDUFA 8-Ks. (17 or 21 UTC are valid alternatives if a later
intraday catch of same-day filings is wanted.)

```sql
-- RUN ONLY AFTER `modal deploy` has shipped bc_universe_pdufa_once.
-- Idempotent: ON CONFLICT (name) keeps re-runs safe.
INSERT INTO public.scanners (
    name,
    tool_path,
    status,
    cadence,
    scheduled_hour_utc,
    default_scoring_profile,
    timeout_soft_s,
    timeout_hard_s,
    config
) VALUES (
    'bc_universe_pdufa',
    'modal_workers/fetchers/universe/bc_universe_pdufa.py',
    'operational',
    'daily',
    13,                                   -- valid dispatch tick (US pre-open)
    'binary_catalyst',                    -- NOT NULL; nominal (this is a fetcher, not a signal scanner)
    1000,                                 -- soft budget (Polygon pacing dominates wall-clock)
    1200,                                 -- hard timeout — mirrors the @app.function timeout
    jsonb_build_object(
        'writes', 'bc_applications,bc_application_features,bc_company_tradeable',
        'purpose', 'BC Light v4 Phase 0 pending-PDUFA universe build (THE GATE)',
        'source', 'edgar_8k_6k_pdufa_extraction',
        'appno_recovery', 'drugsfda_orig_join',
        'window_days', 120,
        'polygon_pace_s', 13,
        'emits_signals', false,
        'fail_loud', 'bc_pipeline_runs'
    )
)
ON CONFLICT (name) DO UPDATE SET
    status             = EXCLUDED.status,
    cadence            = EXCLUDED.cadence,
    scheduled_hour_utc = EXCLUDED.scheduled_hour_utc,
    tool_path          = EXCLUDED.tool_path,
    timeout_soft_s     = EXCLUDED.timeout_soft_s,
    timeout_hard_s     = EXCLUDED.timeout_hard_s,
    config             = EXCLUDED.config,
    updated_at         = now();
```

> No `_FETCHERS_AT_HOUR` edit in `app.py` is needed: that hardcoded map is for the
> catalyst-universe fetchers that have **no** scanners row (they use the `fetch()`
> contract via `_run_fetcher`). `bc_universe_pdufa` uses option (a) — the registry
> row above — so `load_operational_daily_names_for_hour(13)` returns it
> automatically once the row is `operational`.

## 4. Verify the live cron (after both steps)

```sql
-- the registry row is picked up by the 13 UTC dispatch bucket
SELECT name, status, cadence, scheduled_hour_utc, last_run_utc, last_run_status
FROM public.scanners WHERE name = 'bc_universe_pdufa';

-- liveness = today's bc_pipeline_runs row landed (fail-loud invariant)
SELECT pipeline_name, status, snapshot_date, n_processed, n_failed, started_at, finished_at
FROM bc_pipeline_runs
WHERE pipeline_name = 'bc_universe_pdufa'
ORDER BY started_at DESC LIMIT 3;

-- the §4.1 gate query (in-window / in-window-tradeable) off the freshly-written rows
WITH f AS (
  SELECT DISTINCT ON (application_number) application_number, sponsor_cik, appl_type, pdufa_date
  FROM bc_application_features
  ORDER BY application_number, snapshot_date DESC, built_at DESC
), t AS (
  SELECT DISTINCT ON (sponsor_cik) sponsor_cik, market_cap_usd, avg_daily_volume_usd,
         options_chain_exists, borrow_available
  FROM bc_company_tradeable ORDER BY sponsor_cik, snapshot_date DESC
)
SELECT
  count(*) FILTER (WHERE f.appl_type IN ('NDA','BLA') AND f.pdufa_date IS NOT NULL
                   AND (f.pdufa_date - CURRENT_DATE) BETWEEN 0 AND 120)             AS in_window,
  count(*) FILTER (WHERE f.appl_type IN ('NDA','BLA') AND f.pdufa_date IS NOT NULL
                   AND (f.pdufa_date - CURRENT_DATE) BETWEEN 0 AND 120
                   AND COALESCE(t.market_cap_usd,0) >= 250000000
                   AND COALESCE(t.avg_daily_volume_usd,0) >= 2000000
                   AND (COALESCE(t.options_chain_exists,false) OR COALESCE(t.borrow_available,false))) AS in_window_tradeable
FROM f LEFT JOIN t ON t.sponsor_cik = f.sponsor_cik;
```

## 5. Rollback / disable (no redeploy needed)

```sql
UPDATE public.scanners SET status='deprecated', updated_at=now()
WHERE name='bc_universe_pdufa';   -- dispatcher skips non-operational statuses
```

(`status` allows `deprecated`, not `disabled` — memory `v2_teardown_phasing`.)
