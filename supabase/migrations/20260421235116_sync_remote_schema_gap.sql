create extension if not exists "vector" with schema "extensions";

drop trigger if exists "phase3_updated" on "public"."phase3_base_rates";

drop policy "pe_filer_select" on "public"."pe_filer_allowlist";

drop policy "phase3_select" on "public"."phase3_base_rates";

drop view if exists "public"."emissions_ledger";

alter table "public"."scanner_runs" add column "fetched_records" integer;

drop extension if exists "vector";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.call_fanout()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
DECLARE
  webhook_secret text;
BEGIN
  SELECT decrypted_secret INTO webhook_secret
  FROM vault.decrypted_secrets
  WHERE name = 'webhook_secret'
  LIMIT 1;

  PERFORM net.http_post(
    url := 'https://xvwvwbnxdsjpnealarkh.supabase.co/functions/v1/fanout',
    body := jsonb_build_object(
      'type', TG_OP,
      'table', TG_TABLE_NAME,
      'schema', TG_TABLE_SCHEMA,
      'record', row_to_json(NEW),
      'old_record', null
    ),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-supabase-webhook-secret', coalesce(webhook_secret, '')
    ),
    timeout_milliseconds := 30000
  );
  RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.call_reactor()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
DECLARE
  webhook_secret text;
BEGIN
  SELECT decrypted_secret INTO webhook_secret
  FROM vault.decrypted_secrets
  WHERE name = 'webhook_secret'
  LIMIT 1;

  PERFORM net.http_post(
    url := 'https://xvwvwbnxdsjpnealarkh.supabase.co/functions/v1/reactor',
    body := jsonb_build_object(
      'type', TG_OP,
      'table', TG_TABLE_NAME,
      'schema', TG_TABLE_SCHEMA,
      'record', row_to_json(NEW),
      'old_record', null
    ),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-supabase-webhook-secret', coalesce(webhook_secret, '')
    ),
    timeout_milliseconds := 30000
  );
  RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.reporting_integrity_sweep()
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
DECLARE
  v_orphan_alerts int;
  v_stuck_active int;
  v_stuck_drafting int;
BEGIN
  -- 1. Orphan alerts: alerts where the signal has since been re-classified away
  --    from 'immediate'. Expected rare; each is a reactor-cross-update race.
  WITH orphans AS (
    SELECT a.id, a.signal_id, s.band_with_bonus
    FROM public.alerts a
    JOIN public.signals s ON s.signal_id = a.signal_id
    WHERE s.band_with_bonus IS NOT NULL AND s.band_with_bonus <> 'immediate'
    LIMIT 50
  ), inserted_orphans AS (
    INSERT INTO public.operator_flags
      (severity, source, kind, signal_id, title, evidence)
    SELECT
      'warn', 'reporting_weekly', 'orphan_alert', signal_id,
      format('alert %s orphaned: signal band now %s', left(id::text, 8), band_with_bonus),
      jsonb_build_object('alert_id', id, 'new_band', band_with_bonus)
    FROM orphans
    ON CONFLICT DO NOTHING
    RETURNING 1
  )
  SELECT count(*) INTO v_orphan_alerts FROM orphans;

  -- 2. Stuck-active candidates: state='active' with no candidate_events in 45d.
  WITH stuck AS (
    SELECT c.id, c.ticker, c.mic, c.updated_at
    FROM public.candidates c
    WHERE c.state = 'active'
      AND NOT EXISTS (
        SELECT 1 FROM public.candidate_events ce
        WHERE ce.candidate_id = c.id
          AND ce.created_at >= now() - interval '45 days'
      )
    LIMIT 50
  ), inserted_stuck AS (
    INSERT INTO public.operator_flags
      (severity, source, kind, candidate_id, title, evidence)
    SELECT
      'warn', 'reporting_weekly', 'stuck_active_candidate', id,
      format('%s.%s active but no events in 45d', ticker, coalesce(mic, '?')),
      jsonb_build_object('updated_at', updated_at)
    FROM stuck
    ON CONFLICT DO NOTHING
    RETURNING 1
  )
  SELECT count(*) INTO v_stuck_active FROM stuck;

  -- 3. Stuck-drafting thesis_jobs: status='drafting' older than 1 hour.
  WITH stuck_jobs AS (
    SELECT id, signal_id, started_at
    FROM public.thesis_jobs
    WHERE status = 'drafting'
      AND started_at < now() - interval '1 hour'
    LIMIT 20
  ), inserted_jobs AS (
    INSERT INTO public.operator_flags
      (severity, source, kind, signal_id, title, evidence)
    SELECT
      'warn', 'reporting_weekly', 'stuck_drafting_thesis_job', signal_id,
      format('thesis_job %s stuck in drafting since %s', left(id::text, 8), started_at),
      jsonb_build_object('job_id', id)
    FROM stuck_jobs
    ON CONFLICT DO NOTHING
    RETURNING 1
  )
  SELECT count(*) INTO v_stuck_drafting FROM stuck_jobs;

  RETURN jsonb_build_object(
    'orphan_alerts', v_orphan_alerts,
    'stuck_active_candidates', v_stuck_active,
    'stuck_drafting_jobs', v_stuck_drafting,
    'swept_at', now()
  );
END;
$function$
;

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$
;

create or replace view "public"."emissions_ledger" as  SELECT s.signal_id,
    s.scanner_id,
    sc.name AS scanner_name,
    s.scoring_profile AS profile,
    s.entity_id,
    s.issuer_figi,
    e.primary_ticker AS ticker,
    e.primary_mic AS mic,
    s.signal_type,
    s.thesis_direction,
    s.scan_date AS scored_at,
    s.source_date,
    COALESCE(s.score_with_bonus, s.score) AS score_total,
    COALESCE(s.band_with_bonus, s.band) AS band,
    s.dimensions AS dims_json,
    s.auto_caps_triggered,
    s.convergence_bonus,
        CASE
            WHEN (tj.status = 'promoted'::text) THEN 'promoted'::text
            WHEN (tj.status = 'dlq'::text) THEN 'rejected_thesis'::text
            WHEN (tj.status = 'scoring_complete_below_immediate'::text) THEN 'resolved_below_immediate'::text
            WHEN (tj.status = ANY (ARRAY['queued'::text, 'drafting'::text, 'gate_failed_retrying'::text, 'needs_scoring'::text, 'scoring'::text])) THEN 'pending'::text
            WHEN ((COALESCE(s.band_with_bonus, s.band) = ANY (ARRAY['archive'::public.signal_band, 'watchlist'::public.signal_band, 'discard'::public.signal_band])) AND (array_length(s.auto_caps_triggered, 1) > 0)) THEN 'auto_capped'::text
            WHEN (COALESCE(s.band_with_bonus, s.band) = ANY (ARRAY['archive'::public.signal_band, 'watchlist'::public.signal_band, 'discard'::public.signal_band])) THEN 'below_band'::text
            WHEN (COALESCE(s.band_with_bonus, s.band) = 'immediate'::public.signal_band) THEN 'immediate_no_thesis_job'::text
            ELSE 'unknown'::text
        END AS gate_decision,
    jsonb_build_object('auto_caps_triggered', s.auto_caps_triggered, 'thesis_job_status', tj.status, 'thesis_gate_reasons', tj.gate_reasons, 'thesis_attempt_count', tj.attempt_count) AS gate_reason,
    tj.id AS thesis_job_id,
    tj.status AS thesis_job_status,
    tj.candidate_id,
    c.state AS candidate_state,
    c.thesis_approved_at AS promoted_at,
    c.next_catalyst_date AS predicted_catalyst_date,
    o.id AS outcome_id,
    o.outcome_type AS resolution_type,
    o.created_at AS resolution_date,
    o.realized_return
   FROM (((((public.signals s
     LEFT JOIN public.scanners sc ON ((sc.id = s.scanner_id)))
     LEFT JOIN public.entities e ON ((e.id = s.entity_id)))
     LEFT JOIN public.thesis_jobs tj ON ((tj.signal_id = s.signal_id)))
     LEFT JOIN public.candidates c ON ((c.id = tj.candidate_id)))
     LEFT JOIN LATERAL ( SELECT outcomes.id,
            outcomes.candidate_id,
            outcomes.outcome_type,
            outcomes.realized_return,
            outcomes.notes,
            outcomes.created_at
           FROM public.outcomes
          WHERE (outcomes.candidate_id = c.id)
          ORDER BY outcomes.created_at DESC
         LIMIT 1) o ON (true));


CREATE OR REPLACE FUNCTION public.set_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$function$
;


  create policy "failed_reactor_events_deny_authenticated"
  on "public"."failed_reactor_events"
  as permissive
  for all
  to authenticated
using (false)
with check (false);



  create policy "pe_filer_allowlist_select"
  on "public"."pe_filer_allowlist"
  as permissive
  for select
  to authenticated
using (true);



  create policy "phase3_base_rates_select"
  on "public"."phase3_base_rates"
  as permissive
  for select
  to authenticated
using (true);


CREATE TRIGGER alerts_insert_wh AFTER INSERT ON public.alerts FOR EACH ROW EXECUTE FUNCTION public.call_fanout();

CREATE TRIGGER candidate_events_fanout_wh AFTER INSERT ON public.candidate_events FOR EACH ROW EXECUTE FUNCTION public.call_fanout();

CREATE TRIGGER phase3_base_rates_updated BEFORE UPDATE ON public.phase3_base_rates FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER signals_insert_wh AFTER INSERT ON public.signals FOR EACH ROW EXECUTE FUNCTION public.call_reactor();
