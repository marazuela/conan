-- Conan v2 — atomic candidate state transition RPC
--
-- Context:
--   Deterministic lifecycle actors (pre_edge_monitor today; candidate_aging or
--   future operators later) need a single transaction that:
--     1. updates candidates.state
--     2. appends candidate_events(event_type='state_changed')
--     3. appends outcomes for terminal transitions
--   Keeping these writes bundled prevents partial transitions where the state is
--   changed but the audit trail or outcome row is missing.
--
-- Notes:
--   - SECURITY INVOKER (default). Service-role callers already bypass RLS.
--   - Public schema is acceptable here because the function is not security
--     definer and writes only through service-role access.
--   - Returns a jsonb envelope for easy consumption via PostgREST RPC.

CREATE OR REPLACE FUNCTION public.candidate_transition_apply(
  p_candidate_id uuid,
  p_new_state candidate_state,
  p_reason text,
  p_source text,
  p_outcome_type text DEFAULT NULL,
  p_outcome_notes text DEFAULT NULL,
  p_triggered_kill_id text DEFAULT NULL,
  p_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  v_prev_state candidate_state;
  v_event_id uuid;
  v_outcome_id uuid;
BEGIN
  SELECT state
  INTO v_prev_state
  FROM candidates
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

  UPDATE candidates
  SET state = p_new_state,
      updated_at = now()
  WHERE id = p_candidate_id;

  INSERT INTO candidate_events (candidate_id, event_type, payload)
  VALUES (
    p_candidate_id,
    'state_changed',
    jsonb_build_object(
      'from', v_prev_state,
      'to', p_new_state,
      'reason', p_reason,
      'source', p_source
    )
    || COALESCE(p_payload, '{}'::jsonb)
    || CASE
         WHEN p_triggered_kill_id IS NOT NULL
           THEN jsonb_build_object('triggered_kill_id', p_triggered_kill_id)
         ELSE '{}'::jsonb
       END
  )
  RETURNING id INTO v_event_id;

  IF p_outcome_type IS NOT NULL THEN
    INSERT INTO outcomes (candidate_id, outcome_type, notes)
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

COMMENT ON FUNCTION public.candidate_transition_apply(
  uuid, candidate_state, text, text, text, text, text, jsonb
) IS
  'Atomic candidate lifecycle transition helper. Updates candidates.state, appends a '
  'state_changed candidate_event, and optionally appends an outcomes row for terminal '
  'transitions. Intended for deterministic service-role actors such as pre_edge_monitor.';
