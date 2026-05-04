-- Conan dashboard — operator workflow contract.
--
-- Adds the read model and audited mutation RPCs used by the dashboard review
-- queue. The functions are SECURITY DEFINER but explicitly require auth.uid().

-- Read model: one place for display band/score semantics plus lightweight
-- denormalized labels used by dashboard lists.
CREATE OR REPLACE VIEW public.dashboard_signal_rows
WITH (security_invoker = true)
AS
SELECT
  s.signal_id,
  s.entity_id,
  s.issuer_figi,
  s.scanner_id,
  s.scanner_run_id,
  s.scoring_profile,
  s.rubric_version_id,
  s.source_content_hash,
  s.source_url,
  s.source_date,
  s.scan_date,
  s.signal_type,
  s.thesis_direction,
  s.strength_estimate,
  s.imported,
  s.dimensions,
  s.score,
  s.band,
  s.auto_caps_triggered,
  s.convergence_key,
  s.convergence_bonus,
  s.score_with_bonus,
  s.band_with_bonus,
  COALESCE(s.score_with_bonus, s.score) AS display_score,
  COALESCE(s.band_with_bonus, s.band) AS display_band,
  s.convergence_evaluated_at,
  s.raw_payload,
  s.extensions,
  s.created_at,
  e.name AS entity_name,
  e.primary_ticker,
  e.primary_mic,
  sc.name AS scanner_name,
  sc.geography AS scanner_geography,
  sc.cadence AS scanner_cadence
FROM public.signals s
LEFT JOIN public.entities e ON e.id = s.entity_id
LEFT JOIN public.scanners sc ON sc.id = s.scanner_id;

GRANT SELECT ON public.dashboard_signal_rows TO authenticated;

-- Durable audit sink for dashboard mutations that are not fully represented
-- by candidate_events.
CREATE TABLE IF NOT EXISTS public.operator_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid NOT NULL REFERENCES auth.users(id),
  action_type text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  candidate_id uuid REFERENCES public.candidates(id) ON DELETE SET NULL,
  signal_id text REFERENCES public.signals(signal_id) ON DELETE SET NULL,
  thesis_job_id uuid REFERENCES public.thesis_jobs(id) ON DELETE SET NULL,
  flag_id uuid REFERENCES public.operator_flags(id) ON DELETE SET NULL,
  failure_id uuid REFERENCES public.thesis_drafting_failures(id) ON DELETE SET NULL,
  note text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS operator_actions_created_idx
  ON public.operator_actions(created_at DESC);
CREATE INDEX IF NOT EXISTS operator_actions_actor_created_idx
  ON public.operator_actions(actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS operator_actions_candidate_created_idx
  ON public.operator_actions(candidate_id, created_at DESC)
  WHERE candidate_id IS NOT NULL;

ALTER TABLE public.operator_actions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS operator_actions_select ON public.operator_actions;
CREATE POLICY operator_actions_select ON public.operator_actions
  FOR SELECT
  TO authenticated
  USING (true);

ALTER TABLE public.candidate_aging_failures
  ADD COLUMN IF NOT EXISTS resolved_at timestamptz;

CREATE INDEX IF NOT EXISTS candidate_aging_failures_unresolved_idx
  ON public.candidate_aging_failures(attempt_at DESC)
  WHERE resolved_at IS NULL;

-- Allow dashboard-authenticated writes to the curator rationale table; all
-- mutations remain app-audited through RPC/server action side effects.
DROP POLICY IF EXISTS candidate_rationales_write ON public.candidate_rationales;
CREATE POLICY candidate_rationales_write ON public.candidate_rationales
  FOR INSERT
  TO authenticated
  WITH CHECK (true);

DROP POLICY IF EXISTS candidate_rationales_update ON public.candidate_rationales;
CREATE POLICY candidate_rationales_update ON public.candidate_rationales
  FOR UPDATE
  TO authenticated
  USING (true)
  WITH CHECK (true);

-- Preserve existing service-role behavior while allowing dashboard calls to
-- thread the acting user into candidate_events via p_payload.user_id.
CREATE OR REPLACE FUNCTION public.candidate_transition_apply(
  p_candidate_id uuid,
  p_new_state public.candidate_state,
  p_reason text,
  p_source text,
  p_outcome_type text DEFAULT NULL,
  p_outcome_notes text DEFAULT NULL,
  p_triggered_kill_id text DEFAULT NULL,
  p_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_prev_state public.candidate_state;
  v_event_id uuid;
  v_outcome_id uuid;
  v_user_id uuid;
  v_payload jsonb := COALESCE(p_payload, '{}'::jsonb);
BEGIN
  IF v_payload ? 'user_id' THEN
    v_user_id := NULLIF(v_payload->>'user_id', '')::uuid;
    v_payload := v_payload - 'user_id';
  END IF;

  SELECT state
  INTO v_prev_state
  FROM public.candidates
  WHERE id = p_candidate_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'candidate_transition_apply: candidate % not found', p_candidate_id;
  END IF;

  IF v_prev_state = p_new_state THEN
    RETURN jsonb_build_object(
      'applied', false,
      'candidate_id', p_candidate_id,
      'from_state', v_prev_state,
      'to_state', p_new_state,
      'reason', 'no_state_change'
    );
  END IF;

  UPDATE public.candidates
  SET state = p_new_state,
      updated_at = now()
  WHERE id = p_candidate_id;

  INSERT INTO public.candidate_events (candidate_id, event_type, payload, user_id)
  VALUES (
    p_candidate_id,
    'state_changed',
    jsonb_build_object(
      'from', v_prev_state,
      'to', p_new_state,
      'reason', p_reason,
      'source', p_source
    )
    || v_payload
    || CASE
         WHEN p_triggered_kill_id IS NOT NULL
           THEN jsonb_build_object('triggered_kill_id', p_triggered_kill_id)
         ELSE '{}'::jsonb
       END,
    v_user_id
  )
  RETURNING id INTO v_event_id;

  IF p_outcome_type IS NOT NULL THEN
    INSERT INTO public.outcomes (candidate_id, outcome_type, notes)
    VALUES (p_candidate_id, p_outcome_type, p_outcome_notes)
    RETURNING id INTO v_outcome_id;
  END IF;

  RETURN jsonb_build_object(
    'applied', true,
    'candidate_id', p_candidate_id,
    'from_state', v_prev_state,
    'to_state', p_new_state,
    'candidate_event_id', v_event_id,
    'outcome_id', v_outcome_id
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_candidate_set_state(
  p_candidate_id uuid,
  p_new_state public.candidate_state,
  p_reason text,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_current_state public.candidate_state;
  v_result jsonb;
  v_outcome_type text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: authentication required';
  END IF;

  IF p_new_state NOT IN ('active', 'killed') THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: new_state %, expected active or killed', p_new_state;
  END IF;

  SELECT state
  INTO v_current_state
  FROM public.candidates
  WHERE id = p_candidate_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: candidate % not found', p_candidate_id;
  END IF;

  IF v_current_state <> 'watch' THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: candidate % has state %, expected watch', p_candidate_id, v_current_state;
  END IF;

  IF p_new_state IN ('killed', 'delivered') THEN
    v_outcome_type := p_new_state::text;
  END IF;

  v_result := public.candidate_transition_apply(
    p_candidate_id,
    p_new_state,
    COALESCE(NULLIF(p_reason, ''), 'dashboard_review'),
    'dashboard',
    v_outcome_type,
    p_note,
    NULL,
    jsonb_build_object('user_id', v_actor, 'note', p_note)
  );

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'candidate_set_state',
    'candidate',
    p_candidate_id::text,
    p_candidate_id,
    p_note,
    jsonb_build_object('new_state', p_new_state, 'reason', p_reason, 'result', v_result)
  );

  RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_thesis_requeue(
  p_job_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_old public.thesis_jobs%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_thesis_requeue: authentication required';
  END IF;

  SELECT * INTO v_old
  FROM public.thesis_jobs
  WHERE id = p_job_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_thesis_requeue: thesis job % not found', p_job_id;
  END IF;

  IF v_old.status NOT IN ('dlq', 'gate_failed_retrying') THEN
    RAISE EXCEPTION 'dashboard_thesis_requeue: job % has status %, expected dlq or gate_failed_retrying', p_job_id, v_old.status;
  END IF;

  UPDATE public.thesis_jobs
  SET status = 'queued',
      attempt_count = 0,
      challenge_count = 0,
      drafted_thesis = NULL,
      gate_reasons = NULL,
      started_at = NULL,
      completed_at = NULL,
      resolved_at = NULL,
      updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, thesis_job_id, signal_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'thesis_requeue',
    'thesis_job',
    p_job_id::text,
    p_job_id,
    v_old.signal_id,
    v_old.candidate_id,
    p_note,
    jsonb_build_object('previous_status', v_old.status, 'previous_gate_reasons', v_old.gate_reasons)
  );

  RETURN jsonb_build_object('applied', true, 'job_id', p_job_id, 'status', 'queued');
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_thesis_resolve(
  p_job_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_job public.thesis_jobs%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_thesis_resolve: authentication required';
  END IF;

  SELECT * INTO v_job
  FROM public.thesis_jobs
  WHERE id = p_job_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_thesis_resolve: thesis job % not found', p_job_id;
  END IF;

  UPDATE public.thesis_jobs
  SET resolved_at = now(),
      updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, thesis_job_id, signal_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'thesis_resolve',
    'thesis_job',
    p_job_id::text,
    p_job_id,
    v_job.signal_id,
    v_job.candidate_id,
    p_note,
    jsonb_build_object('status', v_job.status)
  );

  RETURN jsonb_build_object('applied', true, 'job_id', p_job_id, 'resolved_at', now());
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_failure_resolve(
  p_failure_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_failure public.thesis_drafting_failures%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_failure_resolve: authentication required';
  END IF;

  SELECT * INTO v_failure
  FROM public.thesis_drafting_failures
  WHERE id = p_failure_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_failure_resolve: failure % not found', p_failure_id;
  END IF;

  UPDATE public.thesis_drafting_failures
  SET resolved_at = now()
  WHERE id = p_failure_id;

  UPDATE public.thesis_jobs
  SET resolved_at = now(),
      updated_at = now()
  WHERE id = v_failure.thesis_job_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, failure_id, thesis_job_id, signal_id, note, payload
  )
  VALUES (
    v_actor,
    'failure_resolve',
    'thesis_drafting_failure',
    p_failure_id::text,
    p_failure_id,
    v_failure.thesis_job_id,
    v_failure.signal_id,
    p_note,
    jsonb_build_object('final_reasons', v_failure.final_reasons)
  );

  RETURN jsonb_build_object('applied', true, 'failure_id', p_failure_id, 'resolved_at', now());
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_flag_resolve(
  p_flag_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_flag public.operator_flags%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_flag_resolve: authentication required';
  END IF;

  SELECT * INTO v_flag
  FROM public.operator_flags
  WHERE id = p_flag_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_flag_resolve: flag % not found', p_flag_id;
  END IF;

  UPDATE public.operator_flags
  SET resolved_at = now(),
      resolved_by = v_actor,
      resolved_note = p_note
  WHERE id = p_flag_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, flag_id, candidate_id, signal_id, note, payload
  )
  VALUES (
    v_actor,
    'flag_resolve',
    'operator_flag',
    p_flag_id::text,
    p_flag_id,
    v_flag.candidate_id,
    v_flag.signal_id,
    p_note,
    jsonb_build_object('source', v_flag.source, 'kind', v_flag.kind, 'severity', v_flag.severity)
  );

  RETURN jsonb_build_object('applied', true, 'flag_id', p_flag_id, 'resolved_at', now());
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_aging_failure_resolve(
  p_failure_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_failure public.candidate_aging_failures%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_aging_failure_resolve: authentication required';
  END IF;

  SELECT * INTO v_failure
  FROM public.candidate_aging_failures
  WHERE id = p_failure_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_aging_failure_resolve: aging failure % not found', p_failure_id;
  END IF;

  UPDATE public.candidate_aging_failures
  SET resolved_at = now()
  WHERE id = p_failure_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'aging_failure_resolve',
    'candidate_aging_failure',
    p_failure_id::text,
    v_failure.candidate_id,
    p_note,
    jsonb_build_object(
      'error_kind', v_failure.error_kind,
      'consecutive_failures', v_failure.consecutive_failures
    )
  );

  RETURN jsonb_build_object('applied', true, 'failure_id', p_failure_id, 'resolved_at', now());
END;
$$;

CREATE OR REPLACE FUNCTION public.dashboard_rationale_upsert(
  p_candidate_id uuid,
  p_ticker text,
  p_one_liner text,
  p_hypothesis text,
  p_thesis text,
  p_expected_outcome text,
  p_price_targets jsonb,
  p_time_sensitivity text,
  p_kill_watch text,
  p_catalyst_date_iso date DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_event_id uuid;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_rationale_upsert: authentication required';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.candidates WHERE id = p_candidate_id) THEN
    RAISE EXCEPTION 'dashboard_rationale_upsert: candidate % not found', p_candidate_id;
  END IF;

  INSERT INTO public.candidate_rationales (
    ticker,
    one_liner,
    hypothesis,
    thesis,
    expected_outcome,
    price_targets,
    time_sensitivity,
    kill_watch,
    catalyst_date_iso,
    archived
  )
  VALUES (
    p_ticker,
    p_one_liner,
    p_hypothesis,
    p_thesis,
    p_expected_outcome,
    COALESCE(p_price_targets, '{}'::jsonb),
    p_time_sensitivity,
    p_kill_watch,
    p_catalyst_date_iso,
    false
  )
  ON CONFLICT (ticker) DO UPDATE
  SET one_liner = EXCLUDED.one_liner,
      hypothesis = EXCLUDED.hypothesis,
      thesis = EXCLUDED.thesis,
      expected_outcome = EXCLUDED.expected_outcome,
      price_targets = EXCLUDED.price_targets,
      time_sensitivity = EXCLUDED.time_sensitivity,
      kill_watch = EXCLUDED.kill_watch,
      catalyst_date_iso = EXCLUDED.catalyst_date_iso,
      archived = false,
      updated_at = now();

  INSERT INTO public.candidate_events (candidate_id, event_type, user_id, payload)
  VALUES (
    p_candidate_id,
    'thesis_updated',
    v_actor,
    jsonb_build_object('source', 'dashboard', 'ticker', p_ticker)
  )
  RETURNING id INTO v_event_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'rationale_upsert',
    'candidate_rationale',
    p_ticker,
    p_candidate_id,
    NULL,
    jsonb_build_object('ticker', p_ticker, 'candidate_event_id', v_event_id)
  );

  RETURN jsonb_build_object('applied', true, 'candidate_id', p_candidate_id, 'ticker', p_ticker, 'candidate_event_id', v_event_id);
END;
$$;

REVOKE ALL ON FUNCTION public.dashboard_candidate_set_state(uuid, public.candidate_state, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_thesis_requeue(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_thesis_resolve(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_failure_resolve(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_flag_resolve(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_aging_failure_resolve(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.dashboard_rationale_upsert(uuid, text, text, text, text, text, jsonb, text, text, date) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.dashboard_candidate_set_state(uuid, public.candidate_state, text, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_thesis_requeue(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_thesis_resolve(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_failure_resolve(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_flag_resolve(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_aging_failure_resolve(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_rationale_upsert(uuid, text, text, text, text, text, jsonb, text, text, date) TO authenticated;

-- Dashboard realtime coverage. Guard each ADD TABLE because publication DDL is
-- not idempotent.
DO $$
DECLARE
  v_table_name text;
  tables text[] := ARRAY[
    'signals',
    'alerts',
    'candidates',
    'thesis_jobs',
    'operator_flags',
    'scanner_runs',
    'candidate_aging_failures'
  ];
BEGIN
  FOREACH v_table_name IN ARRAY tables LOOP
    IF to_regclass('public.' || v_table_name) IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = v_table_name
      )
    THEN
      EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', v_table_name);
    END IF;
  END LOOP;
END $$;
