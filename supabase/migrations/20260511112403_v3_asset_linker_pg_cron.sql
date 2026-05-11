-- v3 asset_linker + fact_extractor scheduling — pg_cron triggers.
--
-- Problem: The v3 orchestrator pipeline has six stages:
--   1. ingestion adapters         → writes to public.documents (scheduled)
--   2. sonnet asset_linker pass-1 → writes to public.asset_documents (NOT scheduled)
--   3. haiku asset_linker pass-2  → updates asset_documents verdicts  (NOT scheduled)
--   4. sonnet fact_extractor      → writes to public.extracted_facts  (NOT scheduled)
--   5. reactor enqueue            → asset_documents INSERT → orchestrator_runs (works)
--   6. orchestrator_drain_queue   → pending → completed assessments (scheduled)
--
-- Stages 2/3/4 are on-demand Modal callables only; nothing fires them on a
-- schedule. Result: documents pile up unlinked. As of 2026-05-11 only 40 of
-- 3,671 documents have asset_documents rows (1.1%), and only 2 of 42 active
-- fda_assets have any linked docs. Without these schedules, ingested
-- documents never reach the assessment pipeline.
--
-- Fix mirrors the v3-orchestrator-drain pattern: pg_cron POSTs to
-- compute_v3 multiplex; the multiplex spawns each fire-and-forget so
-- pg_net returns in <1s while the worker runs up to 3600s. No Modal
-- cron slot consumed.
--
-- Schedules:
--   v3-asset-linker-pass1  every 15 min  Sonnet, 200 docs/batch, $15 budget
--   v3-asset-linker-pass2  :10/:40       Haiku, 200 links/batch, $2 budget
--   v3-fact-extractor      hourly at :20 Sonnet, 200 links/batch, $30 budget
--
-- Offsets prevent same-time fan-out (pass-2 at :10/:40 so it never overlaps
-- with pass-1 fires at :00/:15/:30/:45; fact_extractor at :20 sits between
-- a pass-2 finish and the next pass-1).
--
-- Theoretical max cost ceiling: 4×$15 (pass-1) + 2×$2 (pass-2) + 1×$30
-- (fact_extractor) = $94/hr. Actual steady-state ~$0/hr once backlog drains
-- since each worker exits early when its queue is empty.
--
-- Prereqs (must be true BEFORE this migration is applied):
--   1. conan-v3-orchestrator is redeployed with `asset_linker_run`,
--      `asset_linker_pass2_run`, `fact_extractor_run` registered in
--      COMPUTE_V3_ACTIONS + _dispatch_compute_v3_action. Without that,
--      every pg_cron POST will 400 with "unknown action".
--   2. `internal_config.modal_url_compute_v3` is seeded (true since Phase 4B
--      compute RPCs migration 20260427010000).
--   3. `_conan_modal_post_enqueue` exists (true; created by Phase 4B
--      migration 20260429020000_compute_rpcs_split_call.sql).
--
-- Rollback:
--   select cron.unschedule('v3-asset-linker-pass1');
--   select cron.unschedule('v3-asset-linker-pass2');
--   select cron.unschedule('v3-fact-extractor');
-- Leaves the documents-to-links path without a consumer; trigger each
-- worker manually with `modal run modal_workers/orchestrator_app.py::<fn>`.

create extension if not exists pg_cron with schema extensions cascade;

do $$
declare
  v_existing_jobid bigint;
begin
  -- ------------------------------------------------------------------
  -- pass-1: Sonnet asset_linker over unlinked documents (every 15 min)
  -- ------------------------------------------------------------------
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-asset-linker-pass1';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-asset-linker-pass1',
    '*/15 * * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'asset_linker_run',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );

  -- ------------------------------------------------------------------
  -- pass-2: Haiku verifier over low-confidence pass-1 links (2x/hr)
  -- ------------------------------------------------------------------
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-asset-linker-pass2';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-asset-linker-pass2',
    '10,40 * * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'asset_linker_pass2_run',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );

  -- ------------------------------------------------------------------
  -- fact_extractor: Sonnet structured fact extraction (hourly at :20)
  -- ------------------------------------------------------------------
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-fact-extractor';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-fact-extractor',
    '20 * * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'fact_extractor_run',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );
end
$$;
