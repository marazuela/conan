-- v3 over-firing fix PR-B: orchestrator_enqueue_guard policy function.
--
-- Problem: even with document_set_hash stamped on every trigger (PR-A),
-- the cool-down across triggers is still missing. Same asset can be hit
-- by reactor (new_doc), pg_cron (catalyst_proximity), AND tier-2 sweep
-- (scheduled / tier2_escalation) within hours, each insert succeeding
-- because the partial unique index is only race-safe for *pending* rows.
-- Once a run completes, the next trigger can re-enqueue immediately even
-- though the underlying corpus is unchanged.
--
-- Reactor already has an application-side check (reactor/index.ts:
-- "doc_set_unchanged" branch) but it lives only in TypeScript and is
-- doc-bus-only. We need one policy hub, callable from SQL, Python, and
-- Deno.
--
-- This migration adds `public.orchestrator_enqueue_guard(asset_id,
-- trigger_type, hash) -> jsonb` that returns {skip, reason} based on:
--
--   1. Bypass triggers (manual, operator_refresh, backtest) — always
--      proceed.
--   2. NULL hash — never skip (legacy / empty-corpus path).
--   3. Same-hash assessment within the 6h cool-down window
--      (convergence_assessments with the hash AND superseded_at IS NULL
--      AND created_at > now() - interval '6 hours') — skip.
--   4. Same-hash pending orchestrator_runs row already exists — skip
--      (defensive: the partial unique index also enforces this, but the
--      guard returns a clean {skip,reason} payload instead of an
--      exception so callers can log+proceed).
--
-- Pedro-locked decisions (2026-05-25):
--   - 6h cool-down window
--   - tier2_escalation participates in dedup (not bypassed) — matches
--     migration 20260523123321 which already added it to the index.
--
-- Callers in PR-B (this PR):
--   - v3-catalyst-proximity-sweep pg_cron — guard call inlined into the
--     WHERE clause of the cron INSERT.
--
-- Callers in follow-up commits (also in this PR):
--   - orchestrator_runtime/tier2.py enqueue_tier1_escalation + enqueue_tier2_bulk
--   - supabase/functions/reactor/index.ts processAssetDocument (replaces
--     inline lastAssessment lookup with rpc('orchestrator_enqueue_guard'))
--
-- Rollback: drop function if exists public.orchestrator_enqueue_guard(uuid, text, text);

create or replace function public.orchestrator_enqueue_guard(
  p_asset_id uuid,
  p_trigger_type text,
  p_hash text
) returns jsonb
language plpgsql
stable
security invoker
set search_path = public, pg_temp
as $$
declare
  v_cooldown_hours int := 6;
  v_bypass constant text[] := ARRAY['manual', 'operator_refresh', 'backtest'];
  v_now timestamptz := now();
  v_recent_assessment_id uuid;
  v_pending_run_id uuid;
begin
  -- Step 1: bypass set always proceeds.
  if p_trigger_type = ANY(v_bypass) then
    return jsonb_build_object('skip', false, 'reason', 'bypass_trigger');
  end if;

  -- Step 2: NULL hash means the asset has no material primary corpus to
  -- fingerprint. Let the run proceed so we don't silently strand
  -- catalyst-proximity sweeps on empty-doc assets.
  if p_hash is null then
    return jsonb_build_object('skip', false, 'reason', 'null_hash_no_fingerprint');
  end if;

  -- Step 3: same-hash assessment within the cool-down window. The asset
  -- already has a fresh non-superseded assessment for this exact corpus —
  -- running again can't produce new information.
  select id into v_recent_assessment_id
    from public.convergence_assessments
   where asset_id = p_asset_id
     and document_set_hash = p_hash
     and superseded_at is null
     and created_at > v_now - make_interval(hours => v_cooldown_hours)
   order by created_at desc
   limit 1;

  if v_recent_assessment_id is not null then
    return jsonb_build_object(
      'skip', true,
      'reason', 'same_hash_within_cooldown',
      'recent_assessment_id', v_recent_assessment_id,
      'cooldown_hours', v_cooldown_hours
    );
  end if;

  -- Step 4: same-hash pending run already queued. The partial unique
  -- index orchestrator_runs_pending_content_dedup_idx will reject the
  -- INSERT anyway; surfacing the skip up front avoids the exception
  -- handler path in callers.
  select id into v_pending_run_id
    from public.orchestrator_runs
   where asset_id = p_asset_id
     and document_set_hash = p_hash
     and status = 'pending'
   limit 1;

  if v_pending_run_id is not null then
    return jsonb_build_object(
      'skip', true,
      'reason', 'pending_same_hash',
      'pending_run_id', v_pending_run_id
    );
  end if;

  return jsonb_build_object('skip', false, 'reason', 'ok');
end;
$$;

comment on function public.orchestrator_enqueue_guard(uuid, text, text) is
  'Cross-trigger cool-down policy. Returns {skip, reason} jsonb. Callers '
  '(pg_cron sweeps, Python tier-2 helpers, reactor TS) check before INSERT '
  'to avoid same-hash redundant orchestrator_runs across trigger types. '
  'Bypass: manual / operator_refresh / backtest. Cool-down: 6h vs '
  'convergence_assessments.document_set_hash on non-superseded rows. NULL '
  'hash proceeds (no fingerprint). Locked by 2026-05-25 review of 14d '
  'orchestrator over-fire incident (VRDN 41 runs / 149 collisions).';
