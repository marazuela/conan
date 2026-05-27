-- 20260527000000_persist_assessment_v3_null_id_fix.sql
--
-- Bug: persist_assessment_v3 INSERTs into convergence_assessments using
-- jsonb_populate_record(NULL::public.convergence_assessments, v_assessment).
-- jsonb_populate_record does NOT consult column DEFAULT expressions — missing
-- keys land as NULL. The Python caller (orchestrator_runtime/runtime.py
-- stage_10_persist) does not set `id` on the row dict, so every call since
-- 2026-05-19 has tried to INSERT with id=NULL, tripping NOT NULL on the PK
-- and failing every tier-1 orchestrator_runs entry with
--   supabase 400: {"code":"23502","details":"Failing row contains (null, ...)"}.
--
-- Fix: defensively merge `id := gen_random_uuid()` into the populated payload
-- when the caller didn't supply one. Keep the caller-supplied path so explicit
-- ids (used by the post_mortem_queue stub's reference to the assessment, the
-- supersede backfill, and any external replay tooling) still flow through.
--
-- Rollback path: re-apply 20260513130643_persist_assessment_v3_rpc.sql.

CREATE OR REPLACE FUNCTION public.persist_assessment_v3(payload jsonb)
RETURNS uuid
LANGUAGE plpgsql
AS $function$
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

  -- (1) idempotency
  IF v_orchestrator_run_id IS NOT NULL THEN
    SELECT id INTO v_existing_id
    FROM public.convergence_assessments
    WHERE orchestrator_run_id = v_orchestrator_run_id;
    IF v_existing_id IS NOT NULL THEN
      RETURN v_existing_id;
    END IF;
  END IF;

  -- (2) supersede prior live
  UPDATE public.convergence_assessments
  SET superseded_at = now()
  WHERE asset_id = v_asset_id
    AND superseded_at IS NULL;

  -- (3) INSERT parent
  -- Defensive id assignment: jsonb_populate_record ignores column DEFAULTs,
  -- so we must supply gen_random_uuid() when the caller's payload omits id
  -- (or passes empty string).
  INSERT INTO public.convergence_assessments
  SELECT (jsonb_populate_record(
      NULL::public.convergence_assessments,
      v_assessment
        || jsonb_build_object('orchestrator_run_id', v_orchestrator_run_id)
        || jsonb_build_object(
             'id',
             COALESCE(
               NULLIF(v_assessment->>'id', '')::uuid,
               gen_random_uuid()
             )
           )
    )).*
  RETURNING id INTO v_assessment_id;

  -- (3b) backfill superseded_by
  UPDATE public.convergence_assessments
  SET superseded_by = v_assessment_id
  WHERE asset_id = v_asset_id
    AND superseded_at IS NOT NULL
    AND superseded_by IS NULL
    AND id <> v_assessment_id;

  -- (4) assessment_stage_metrics
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

  -- (5) hypothesis_enumeration
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

  -- (6) premortem_assessments
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
$function$;
