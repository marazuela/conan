-- dashboard_thesis_revise(p_job_id uuid, p_drafted_thesis jsonb, p_note text)
--
-- Lets the dashboard reviewer overwrite an AI-drafted thesis and re-queue
-- the job for re-gating. Mirrors the audit + permissioning pattern used by
-- dashboard_thesis_requeue (SECURITY DEFINER, requires auth.uid()).
--
-- The function:
--   1. Locks the target row, validates the job is not already resolved.
--   2. Replaces drafted_thesis with the supplied JSON.
--   3. Resets gate_reasons to NULL so the next gate run re-evaluates the
--      revision from scratch.
--   4. Sets status to 'queued' and bumps attempt_count so the thesis writer
--      treats this as another attempt.
--   5. Clears completed_at + resolved_at so the job is picked back up by
--      whichever worker handles 'queued'.
--   6. Inserts a single operator_actions row with action_type='thesis_revise'
--      and a payload describing the prior state, so the audit trail can
--      reconstruct what the operator changed and from where.

CREATE OR REPLACE FUNCTION public.dashboard_thesis_revise(
  p_job_id          uuid,
  p_drafted_thesis  jsonb,
  p_note            text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_actor uuid := auth.uid();
  v_old   public.thesis_jobs%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_thesis_revise: authentication required';
  END IF;

  IF p_drafted_thesis IS NULL OR jsonb_typeof(p_drafted_thesis) <> 'object' THEN
    RAISE EXCEPTION 'dashboard_thesis_revise: drafted_thesis must be a JSON object';
  END IF;

  SELECT * INTO v_old FROM public.thesis_jobs WHERE id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_thesis_revise: thesis job % not found', p_job_id;
  END IF;

  IF v_old.status = 'promoted' THEN
    RAISE EXCEPTION
      'dashboard_thesis_revise: job % is already promoted; create a new signal to revise',
      p_job_id;
  END IF;

  UPDATE public.thesis_jobs
     SET drafted_thesis = p_drafted_thesis,
         status         = 'queued',
         attempt_count  = COALESCE(v_old.attempt_count, 0) + 1,
         gate_reasons   = NULL,
         completed_at   = NULL,
         resolved_at    = NULL,
         updated_at     = now()
   WHERE id = p_job_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id,
    thesis_job_id, signal_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'thesis_revise',
    'thesis_job',
    p_job_id::text,
    p_job_id,
    v_old.signal_id,
    v_old.candidate_id,
    p_note,
    jsonb_build_object(
      'previous_status',         v_old.status,
      'previous_attempt_count',  v_old.attempt_count,
      'previous_gate_reasons',   v_old.gate_reasons,
      'previous_drafted_thesis', v_old.drafted_thesis
    )
  );

  RETURN jsonb_build_object(
    'applied',        true,
    'job_id',         p_job_id,
    'status',         'queued',
    'attempt_count',  COALESCE(v_old.attempt_count, 0) + 1
  );
END;
$function$;

GRANT EXECUTE ON FUNCTION public.dashboard_thesis_revise(uuid, jsonb, text) TO authenticated;
