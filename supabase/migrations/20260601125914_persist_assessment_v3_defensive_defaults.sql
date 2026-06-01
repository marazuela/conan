-- 20260613020000_persist_assessment_v3_defensive_defaults.sql
--
-- Fix: persist_assessment_v3 23502 NOT NULL violations on Tier-1 v3/v4 writes.
--
-- Root cause:
--   The RPC's INSERT pattern is
--     INSERT INTO convergence_assessments
--     SELECT (jsonb_populate_record(NULL::convergence_assessments, v_assessment)).*
--   `jsonb_populate_record` returns NULL for any column not present in the
--   jsonb. The subsequent `INSERT ... SELECT (record).*` carries those NULLs
--   into the INSERT as explicit values, which bypasses column DEFAULTs (a
--   DEFAULT only fires when the column is omitted from the INSERT column
--   list, not when NULL is supplied). Result: every NOT NULL column with a
--   DEFAULT that Python's row dict doesn't explicitly supply causes 23502.
--
--   stage_10_persist (runtime.py) historically omitted `id`, `created_at`,
--   `tier`, and `constitutional_retries`. The `id` case was hotfixed in the
--   live function with a defensive COALESCE (not on disk) so the failures
--   migrated to whichever NOT NULL column appears next in ordinal order
--   without a value (constitutional_retries → created_at → tier). Symptom:
--   orchestrator_runs.error_message rows like
--     `23502 Failing row contains (null, <asset>, orch-v0.4.0-mvp, ...)`
--   on 2026-05-25/26 (id NULL, before live hotfix) and the analogous next-
--   column failure on 2026-06-01 (post-id-hotfix) on v4 runs.
--
-- Fix:
--   1. Persist this disk-side: brings the migration file back in sync with
--      live (the live id COALESCE was never written to disk).
--   2. Extend the defensive defaults to also cover `created_at` (DB clock,
--      shouldn't be supplied by Python), `tier`, and `constitutional_retries`
--      (orchestrator-owned, also supplied by the matching Python patch in
--      runtime.py — belt-and-suspenders).
--   3. Use the `defaults || v_assessment` merge so caller-supplied values
--      always win; defaults only fire when the key is absent.
--
-- Related: orchestrator_runtime/runtime.py stage_10_persist row dict now
-- includes `tier=1` and `constitutional_retries=0` explicitly. Either side
-- alone is sufficient; both together is defense-in-depth against drift if a
-- new caller forgets one of these keys.
--
-- Rollback: re-apply 20260528000000_persist_assessment_v3_rpc.sql to revert
-- to the pre-hotfix function definition. Doing so will re-introduce the
-- 23502 failure for any caller that omits id/tier/constitutional_retries.

BEGIN;

CREATE OR REPLACE FUNCTION public.persist_assessment_v3(payload jsonb)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
  v_orchestrator_run_id uuid;
  v_asset_id            uuid;
  v_assessment_id       uuid;
  v_existing_id         uuid;
  v_assessment          jsonb;
  v_stage_metrics       jsonb;
  v_hypotheses          jsonb;
  v_premortem           jsonb;
  v_post_mortem         jsonb;
  v_row                 jsonb;
BEGIN
  v_orchestrator_run_id := (payload->>'orchestrator_run_id')::uuid;
  v_assessment          := payload->'assessment';
  v_stage_metrics       := COALESCE(payload->'stage_metrics', '[]'::jsonb);
  v_hypotheses          := COALESCE(payload->'hypotheses', '[]'::jsonb);
  v_premortem           := COALESCE(payload->'premortem_verdicts', '[]'::jsonb);
  v_post_mortem         := payload->'post_mortem_stub';

  IF v_assessment IS NULL THEN
    RAISE EXCEPTION 'persist_assessment_v3: payload.assessment is required';
  END IF;
  v_asset_id := (v_assessment->>'asset_id')::uuid;
  IF v_asset_id IS NULL THEN
    RAISE EXCEPTION 'persist_assessment_v3: assessment.asset_id is required';
  END IF;

  -- (1) idempotency — if the orchestrator_run_id already has an assessment,
  --     return that id. Caller treats the second call as a no-op success.
  IF v_orchestrator_run_id IS NOT NULL THEN
    SELECT id INTO v_existing_id
    FROM public.convergence_assessments
    WHERE orchestrator_run_id = v_orchestrator_run_id;
    IF v_existing_id IS NOT NULL THEN
      RETURN v_existing_id;
    END IF;
  END IF;

  -- (2) supersede prior live assessment for this asset.
  UPDATE public.convergence_assessments
  SET superseded_at = now()
  WHERE asset_id = v_asset_id
    AND superseded_at IS NULL;

  -- (3) INSERT parent. The `defaults || v_assessment` merge guarantees the
  --     four NOT NULL-with-default columns are present in the populated
  --     record, so the INSERT can't 23502 on them. Caller values override
  --     defaults (right side of || wins for shared keys). The trailing
  --     `|| jsonb_build_object('orchestrator_run_id', ...)` forces the
  --     idempotency key onto every insert regardless of caller payload.
  INSERT INTO public.convergence_assessments
  SELECT (jsonb_populate_record(
      NULL::public.convergence_assessments,
      jsonb_build_object(
        'id', gen_random_uuid(),
        'created_at', now(),
        'tier', 1,
        'constitutional_retries', 0
      )
      || v_assessment
      || jsonb_build_object('orchestrator_run_id', v_orchestrator_run_id)
    )).*
  RETURNING id INTO v_assessment_id;

  -- (3b) backfill superseded_by on the rows we just stamped in step (2) so
  --      the supersedence chain is queryable.
  UPDATE public.convergence_assessments
  SET superseded_by = v_assessment_id
  WHERE asset_id = v_asset_id
    AND superseded_at IS NOT NULL
    AND superseded_by IS NULL
    AND id <> v_assessment_id;

  -- (4) assessment_stage_metrics rows
  IF jsonb_array_length(v_stage_metrics) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_stage_metrics)
    LOOP
      INSERT INTO public.assessment_stage_metrics
      SELECT (jsonb_populate_record(
          NULL::public.assessment_stage_metrics,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (5) hypothesis_enumeration rows (one per hypothesis)
  IF jsonb_array_length(v_hypotheses) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_hypotheses)
    LOOP
      INSERT INTO public.hypothesis_enumeration
      SELECT (jsonb_populate_record(
          NULL::public.hypothesis_enumeration,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (6) premortem_assessments rows
  IF jsonb_array_length(v_premortem) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_premortem)
    LOOP
      INSERT INTO public.premortem_assessments
      SELECT (jsonb_populate_record(
          NULL::public.premortem_assessments,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (7) post_mortem_queue stub
  IF v_post_mortem IS NOT NULL THEN
    INSERT INTO public.post_mortem_queue
    SELECT (jsonb_populate_record(
        NULL::public.post_mortem_queue,
        v_post_mortem || jsonb_build_object('assessment_id', v_assessment_id)
      )).*;
  END IF;

  RETURN v_assessment_id;
END;
$$;

COMMENT ON FUNCTION public.persist_assessment_v3(jsonb) IS
  'Wave 4 deep-fix Phase B.1 — atomic Stage 10 writeback. Single transaction '
  'over parent + stage_metrics + hypothesis_enumeration + premortem_assessments '
  '+ post_mortem_queue. Idempotent on orchestrator_run_id (retry returns the '
  'same assessment id). Supersedes prior live assessment for the same asset. '
  '20260613020000: defensive defaults for id / created_at / tier / '
  'constitutional_retries — the jsonb_populate_record pattern bypasses column '
  'DEFAULTs by inserting explicit NULLs, so any NOT NULL DEFAULT column the '
  'caller omits would 23502 without these overrides.';

GRANT EXECUTE ON FUNCTION public.persist_assessment_v3(jsonb)
  TO service_role, authenticated;

COMMIT;
