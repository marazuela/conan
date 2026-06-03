-- dashboard_reactor_event_resolve
--
-- Operator one-click "resolve" for a dead-letter reactor event, used by the
-- /operator "Needs action" queue and the /operator/dlq list. Mirrors the other
-- dashboard_* security-definer RPCs (20260503090000_dashboard_operator_workflow):
-- auth.uid() guard, row lock, audited via operator_actions.
--
-- failed_reactor_events has no dedicated operator_actions FK column, and its
-- signal_id may not reference a persisted signals row (the event IS a
-- processing failure), so the audit row records the event id via target_id and
-- keeps signal_id / diagnostics in the JSONB payload rather than the FK column.
--
-- Also ensures failed_reactor_events is in the supabase_realtime publication so
-- the operator queue drops the row live the moment it is resolved.

CREATE OR REPLACE FUNCTION public.dashboard_reactor_event_resolve(
  p_event_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_old public.failed_reactor_events%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_reactor_event_resolve: authentication required';
  END IF;

  SELECT * INTO v_old
  FROM public.failed_reactor_events
  WHERE id = p_event_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_reactor_event_resolve: reactor event % not found', p_event_id;
  END IF;

  IF v_old.resolved_at IS NOT NULL THEN
    RAISE EXCEPTION 'dashboard_reactor_event_resolve: event % already resolved at %', p_event_id, v_old.resolved_at;
  END IF;

  UPDATE public.failed_reactor_events
  SET resolved_at = now()
  WHERE id = p_event_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'reactor_event_resolve',
    'failed_reactor_event',
    p_event_id::text,
    p_note,
    jsonb_build_object(
      'signal_id', v_old.signal_id,
      'attempt_count', v_old.attempt_count,
      'error_message', v_old.error_message
    )
  );

  RETURN jsonb_build_object('applied', true, 'event_id', p_event_id, 'resolved', true);
END;
$$;

REVOKE ALL ON FUNCTION public.dashboard_reactor_event_resolve(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.dashboard_reactor_event_resolve(uuid, text) TO authenticated;

-- Ensure the operator queue can react live to resolves.
DO $$
DECLARE
  v_table_name text;
  tables text[] := ARRAY['failed_reactor_events'];
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
