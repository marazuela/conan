-- Shared skill-run tracker.
--
-- Local Cursor/Cowork skills are an operational dependency, but some of them
-- only left evidence in downstream side effects. This ledger gives every skill
-- one common start/heartbeat/finish surface, and lets a watchdog alert when a
-- skill is silent or stuck in a running state.

CREATE TABLE IF NOT EXISTS public.skill_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_name text NOT NULL CHECK (length(trim(skill_name)) > 0),
  skill_host text NOT NULL DEFAULT 'cursor-agent' CHECK (length(trim(skill_host)) > 0),
  skill_home text,
  trigger_source text,
  run_key text,
  status text NOT NULL DEFAULT 'running' CHECK (
    status IN ('running', 'completed', 'failed', 'skipped', 'cancelled', 'timeout')
  ),
  started_at timestamptz NOT NULL DEFAULT now(),
  last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  items_seen int NOT NULL DEFAULT 0 CHECK (items_seen >= 0),
  items_processed int NOT NULL DEFAULT 0 CHECK (items_processed >= 0),
  items_succeeded int NOT NULL DEFAULT 0 CHECK (items_succeeded >= 0),
  items_failed int NOT NULL DEFAULT 0 CHECK (items_failed >= 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT skill_runs_terminal_completed_at CHECK (
    (status = 'running' AND completed_at IS NULL)
    OR (status <> 'running' AND completed_at IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS skill_runs_run_key_unique
  ON public.skill_runs (skill_name, run_key)
  WHERE run_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS skill_runs_skill_started_idx
  ON public.skill_runs (skill_name, started_at DESC);

CREATE INDEX IF NOT EXISTS skill_runs_status_heartbeat_idx
  ON public.skill_runs (status, last_heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS skill_runs_completed_idx
  ON public.skill_runs (completed_at DESC NULLS FIRST);

DROP TRIGGER IF EXISTS skill_runs_updated ON public.skill_runs;
CREATE TRIGGER skill_runs_updated
  BEFORE UPDATE ON public.skill_runs
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.skill_runs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS skill_runs_select ON public.skill_runs;
CREATE POLICY skill_runs_select
  ON public.skill_runs
  FOR SELECT
  TO authenticated
  USING (true);

COMMENT ON TABLE public.skill_runs IS
  'Shared execution ledger for local Cursor/Cowork skills. Skills write start, '
  'heartbeat, and terminal rows so silent failures can be detected without '
  'inferring health solely from downstream side effects.';

COMMENT ON COLUMN public.skill_runs.run_key IS
  'Optional idempotency key from the caller, e.g. date bucket, queue batch, or '
  'agent session id. Unique per skill when present.';


CREATE TABLE IF NOT EXISTS public.skill_run_expectations (
  skill_name text PRIMARY KEY CHECK (length(trim(skill_name)) > 0),
  skill_host text NOT NULL DEFAULT 'cursor-agent' CHECK (length(trim(skill_host)) > 0),
  enabled boolean NOT NULL DEFAULT true,
  expected_interval interval,
  max_silence interval,
  stale_running_after interval NOT NULL DEFAULT interval '2 hours',
  severity text NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'critical')),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT skill_run_expectations_positive_intervals CHECK (
    (expected_interval IS NULL OR expected_interval > interval '0 seconds')
    AND (max_silence IS NULL OR max_silence > interval '0 seconds')
    AND stale_running_after > interval '0 seconds'
  )
);

DROP TRIGGER IF EXISTS skill_run_expectations_updated ON public.skill_run_expectations;
CREATE TRIGGER skill_run_expectations_updated
  BEFORE UPDATE ON public.skill_run_expectations
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.skill_run_expectations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS skill_run_expectations_select ON public.skill_run_expectations;
CREATE POLICY skill_run_expectations_select
  ON public.skill_run_expectations
  FOR SELECT
  TO authenticated
  USING (true);

COMMENT ON TABLE public.skill_run_expectations IS
  'Per-skill alert thresholds. max_silence detects scheduled skills that stop '
  'running; stale_running_after detects runs that start but stop heartbeating.';


CREATE OR REPLACE FUNCTION public.skill_run_start(
  p_skill_name text,
  p_skill_host text DEFAULT 'cursor-agent',
  p_skill_home text DEFAULT NULL,
  p_trigger_source text DEFAULT NULL,
  p_run_key text DEFAULT NULL,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_run_id uuid;
BEGIN
  IF NULLIF(trim(p_skill_name), '') IS NULL THEN
    RAISE EXCEPTION 'skill_run_start requires p_skill_name';
  END IF;

  INSERT INTO public.skill_runs (
    skill_name,
    skill_host,
    skill_home,
    trigger_source,
    run_key,
    status,
    started_at,
    last_heartbeat_at,
    completed_at,
    metadata,
    error_message
  )
  VALUES (
    trim(p_skill_name),
    COALESCE(NULLIF(trim(p_skill_host), ''), 'cursor-agent'),
    p_skill_home,
    p_trigger_source,
    NULLIF(trim(p_run_key), ''),
    'running',
    now(),
    now(),
    NULL,
    COALESCE(p_metadata, '{}'::jsonb),
    NULL
  )
  ON CONFLICT (skill_name, run_key) WHERE run_key IS NOT NULL
  DO UPDATE SET
    skill_host = EXCLUDED.skill_host,
    skill_home = EXCLUDED.skill_home,
    trigger_source = EXCLUDED.trigger_source,
    status = 'running',
    started_at = now(),
    last_heartbeat_at = now(),
    completed_at = NULL,
    items_seen = 0,
    items_processed = 0,
    items_succeeded = 0,
    items_failed = 0,
    metadata = public.skill_runs.metadata || EXCLUDED.metadata,
    error_message = NULL
  RETURNING id INTO v_run_id;

  RETURN v_run_id;
END;
$$;


CREATE OR REPLACE FUNCTION public.skill_run_heartbeat(
  p_run_id uuid,
  p_metadata jsonb DEFAULT NULL,
  p_items_seen int DEFAULT NULL,
  p_items_processed int DEFAULT NULL,
  p_items_succeeded int DEFAULT NULL,
  p_items_failed int DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  UPDATE public.skill_runs
     SET last_heartbeat_at = now(),
         metadata = CASE
           WHEN p_metadata IS NULL THEN metadata
           ELSE metadata || p_metadata
         END,
         items_seen = COALESCE(p_items_seen, items_seen),
         items_processed = COALESCE(p_items_processed, items_processed),
         items_succeeded = COALESCE(p_items_succeeded, items_succeeded),
         items_failed = COALESCE(p_items_failed, items_failed)
   WHERE id = p_run_id
     AND status = 'running';

  IF NOT FOUND THEN
    RAISE EXCEPTION 'skill_run_heartbeat could not find running run_id %', p_run_id;
  END IF;
END;
$$;


CREATE OR REPLACE FUNCTION public.skill_run_finish(
  p_run_id uuid,
  p_status text DEFAULT 'completed',
  p_metadata jsonb DEFAULT NULL,
  p_error_message text DEFAULT NULL,
  p_items_seen int DEFAULT NULL,
  p_items_processed int DEFAULT NULL,
  p_items_succeeded int DEFAULT NULL,
  p_items_failed int DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  IF p_status NOT IN ('completed', 'failed', 'skipped', 'cancelled', 'timeout') THEN
    RAISE EXCEPTION 'skill_run_finish received invalid terminal status %', p_status;
  END IF;

  UPDATE public.skill_runs
     SET status = p_status,
         completed_at = now(),
         last_heartbeat_at = now(),
         metadata = CASE
           WHEN p_metadata IS NULL THEN metadata
           ELSE metadata || p_metadata
         END,
         error_message = p_error_message,
         items_seen = COALESCE(p_items_seen, items_seen),
         items_processed = COALESCE(p_items_processed, items_processed),
         items_succeeded = COALESCE(p_items_succeeded, items_succeeded),
         items_failed = COALESCE(p_items_failed, items_failed)
   WHERE id = p_run_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'skill_run_finish could not find run_id %', p_run_id;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.skill_run_start(text, text, text, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.skill_run_heartbeat(uuid, jsonb, int, int, int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.skill_run_finish(uuid, text, jsonb, text, int, int, int, int) FROM PUBLIC;


CREATE OR REPLACE VIEW public.v_skill_run_health
WITH (security_invoker = true) AS
WITH last_runs AS (
  SELECT
    e.skill_name,
    max(r.started_at) AS last_started_at,
    max(r.completed_at) FILTER (WHERE r.status <> 'running') AS last_finished_at,
    max(r.completed_at) FILTER (WHERE r.status IN ('completed', 'skipped')) AS last_success_at,
    max(GREATEST(r.started_at, r.last_heartbeat_at, COALESCE(r.completed_at, r.started_at))) AS last_seen_at,
    count(*) FILTER (WHERE r.status = 'running')::int AS running_count,
    count(*) FILTER (
      WHERE r.status = 'running'
        AND r.last_heartbeat_at < now() - e.stale_running_after
    )::int AS stale_running_count
  FROM public.skill_run_expectations e
  LEFT JOIN public.skill_runs r ON r.skill_name = e.skill_name
  GROUP BY e.skill_name, e.stale_running_after
)
SELECT
  e.skill_name,
  e.skill_host,
  e.enabled,
  e.expected_interval,
  e.max_silence,
  e.stale_running_after,
  e.severity,
  lr.last_started_at,
  lr.last_finished_at,
  lr.last_success_at,
  lr.last_seen_at,
  COALESCE(lr.running_count, 0) AS running_count,
  COALESCE(lr.stale_running_count, 0) AS stale_running_count,
  CASE
    WHEN e.enabled
      AND e.max_silence IS NOT NULL
      AND COALESCE(lr.last_seen_at, '-infinity'::timestamptz) < now() - e.max_silence
      THEN 'silent'
    WHEN e.enabled
      AND COALESCE(lr.stale_running_count, 0) > 0
      THEN 'stale_running'
    ELSE 'ok'
  END AS health_status
FROM public.skill_run_expectations e
LEFT JOIN last_runs lr ON lr.skill_name = e.skill_name;

COMMENT ON VIEW public.v_skill_run_health IS
  'Current health projection for tracked skills. Silent means no recent run or '
  'heartbeat within max_silence; stale_running means at least one running row '
  'has not heartbeated within stale_running_after.';


CREATE OR REPLACE FUNCTION public._skill_run_watchdog()
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_row record;
  v_silent int := 0;
  v_stale int := 0;
BEGIN
  FOR v_row IN
    SELECT *
    FROM public.v_skill_run_health
    WHERE enabled
      AND health_status = 'silent'
  LOOP
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      v_row.severity,
      'skill_watchdog',
      'skill_silent:' || v_row.skill_name,
      'Skill has not reported a recent run',
      v_row.skill_name || ' has no skill_runs heartbeat within its max_silence window.',
      jsonb_build_object(
        'skill_name', v_row.skill_name,
        'skill_host', v_row.skill_host,
        'last_seen_at', v_row.last_seen_at,
        'max_silence_seconds', extract(epoch from v_row.max_silence)::int,
        'expected_interval_seconds', CASE
          WHEN v_row.expected_interval IS NULL THEN NULL
          ELSE extract(epoch from v_row.expected_interval)::int
        END
      )
    )
    ON CONFLICT DO NOTHING;

    v_silent := v_silent + 1;
  END LOOP;

  FOR v_row IN
    SELECT *
    FROM public.v_skill_run_health
    WHERE enabled
      AND stale_running_count > 0
  LOOP
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      v_row.severity,
      'skill_watchdog',
      'skill_stale_running:' || v_row.skill_name,
      'Skill run appears stuck',
      v_row.skill_name || ' has running skill_runs rows with stale heartbeats.',
      jsonb_build_object(
        'skill_name', v_row.skill_name,
        'skill_host', v_row.skill_host,
        'stale_running_count', v_row.stale_running_count,
        'running_count', v_row.running_count,
        'stale_running_after_seconds', extract(epoch from v_row.stale_running_after)::int,
        'last_seen_at', v_row.last_seen_at
      )
    )
    ON CONFLICT DO NOTHING;

    v_stale := v_stale + 1;
  END LOOP;

  UPDATE public.operator_flags f
     SET resolved_at = now(),
         resolved_note = 'auto-resolved by _skill_run_watchdog: skill reported within max_silence',
         updated_at = now()
   WHERE f.source = 'skill_watchdog'
     AND f.kind LIKE 'skill_silent:%'
     AND f.resolved_at IS NULL
     AND NOT EXISTS (
       SELECT 1
       FROM public.v_skill_run_health h
       WHERE h.skill_name = replace(f.kind, 'skill_silent:', '')
         AND h.enabled
         AND h.health_status = 'silent'
     );

  UPDATE public.operator_flags f
     SET resolved_at = now(),
         resolved_note = 'auto-resolved by _skill_run_watchdog: no stale running skill rows',
         updated_at = now()
   WHERE f.source = 'skill_watchdog'
     AND f.kind LIKE 'skill_stale_running:%'
     AND f.resolved_at IS NULL
     AND NOT EXISTS (
       SELECT 1
       FROM public.v_skill_run_health h
       WHERE h.skill_name = replace(f.kind, 'skill_stale_running:', '')
         AND h.enabled
         AND h.stale_running_count > 0
     );

  RETURN jsonb_build_object(
    'skill_silent', v_silent,
    'skill_stale_running', v_stale
  );
END;
$$;

COMMENT ON FUNCTION public._skill_run_watchdog() IS
  'Checks skill_run_expectations against skill_runs and writes operator_flags '
  'for silent scheduled skills and stale running executions.';


DO $$
DECLARE
  v_jobid bigint;
BEGIN
  SELECT jobid INTO v_jobid
    FROM cron.job
   WHERE jobname = 'skill-run-watchdog';

  IF v_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_jobid);
  END IF;

  PERFORM cron.schedule(
    'skill-run-watchdog',
    '*/15 * * * *',
    'SELECT public._skill_run_watchdog();'
  );
END $$;

