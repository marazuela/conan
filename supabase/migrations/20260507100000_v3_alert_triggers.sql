-- Conan v3 — operator delivery rebind (Stream 1).
--
-- Two new database-webhook triggers complete the v3 alert path:
--   1. asset_documents AFTER INSERT (link_type='primary' AND is_material=true)
--      → call_reactor_assetdoc() → POSTs to reactor edge function with
--      table='asset_documents'. Reactor enqueues an orchestrator_runs row.
--   2. convergence_assessments AFTER INSERT (band='immediate' AND
--      superseded_by IS NULL) → call_fanout_assessment() → POSTs to fanout
--      edge function with table='convergence_assessments'. Fanout renders
--      and sends the v3 immediate email.
--
-- Plus an additive schema change on alert_deliveries so the v3 email path
-- can dedupe + audit by assessment_id (the v2 schema only had alert_id and
-- candidate_event_id parents).
--
-- Reactor signals→FDA path: the existing signals_insert_wh trigger continues
-- to fire call_reactor() for non-FDA profiles (activist_governance,
-- takeover_candidate, litigation, etc.). The reactor edge function itself
-- branches on payload.table and on signal.scoring_profile to either run the
-- legacy convergence path or enqueue orchestrator_runs — no schema change
-- required at the trigger layer.
--
-- All changes are idempotent + reversible via standard ALTER TABLE / DROP
-- TRIGGER. The new triggers fire only on the documented predicate, so v2
-- traffic is unaffected.

-- ---------------------------------------------------------------------------
-- 1) alert_deliveries.assessment_id — new audit-parent column for v3.
-- ---------------------------------------------------------------------------
ALTER TABLE public.alert_deliveries
  ADD COLUMN IF NOT EXISTS assessment_id uuid
    REFERENCES public.convergence_assessments(id) ON DELETE CASCADE;

COMMENT ON COLUMN public.alert_deliveries.assessment_id IS
  'v3: audit-parent for fanout dispatches triggered by convergence_assessments band=immediate. Mutually exclusive with alert_id and candidate_event_id (one parent per delivery row).';

-- Permanent dedupe on (assessment_id, channel, target). An assessment_id
-- identifies one orchestrator pass; if a band-flip UPDATE fires the trigger
-- again, or a webhook retry double-delivers, the same recipient never gets
-- the same assessment a second time. This is stricter than the v2
-- alerts.signal_fingerprint per-day dedupe, but appropriate for v3:
-- assessment_id is the natural unit of "this pipeline output."
-- (date_trunc on timestamptz is STABLE, not IMMUTABLE, so it cannot be in
-- an index expression — and the per-day partition isn't needed here.)
CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_assessment_dedup_idx
  ON public.alert_deliveries (assessment_id, channel, target)
  WHERE assessment_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2) call_fanout_assessment() — webhook dispatcher for convergence_assessments.
-- Mirrors call_fanout() (vault webhook secret + net.http_post + 30s timeout).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.call_fanout_assessment()
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
      'old_record', NULL
    ),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-supabase-webhook-secret', coalesce(webhook_secret, '')
    ),
    timeout_milliseconds := 30000
  );
  RETURN NEW;
END;
$function$;

COMMENT ON FUNCTION public.call_fanout_assessment() IS
  'v3 Stream 1: dispatch convergence_assessments AFTER INSERT to fanout edge function. Predicate (band=immediate AND superseded_by IS NULL) is enforced by the trigger WHEN clause, not in the function body.';

-- AFTER INSERT trigger; only fires on rows that ship to operators.
DROP TRIGGER IF EXISTS convergence_assessments_immediate_fanout_wh
  ON public.convergence_assessments;
CREATE TRIGGER convergence_assessments_immediate_fanout_wh
AFTER INSERT ON public.convergence_assessments
FOR EACH ROW
WHEN (NEW.band = 'immediate' AND NEW.superseded_by IS NULL)
EXECUTE FUNCTION public.call_fanout_assessment();

-- AFTER UPDATE trigger: catches the band-flip case (a non-immediate row
-- being upgraded by a later orchestrator pass). Fires only when the band
-- transitioned INTO 'immediate' so re-stamps don't re-email.
DROP TRIGGER IF EXISTS convergence_assessments_band_flip_fanout_wh
  ON public.convergence_assessments;
CREATE TRIGGER convergence_assessments_band_flip_fanout_wh
AFTER UPDATE ON public.convergence_assessments
FOR EACH ROW
WHEN (
  NEW.band = 'immediate'
  AND NEW.superseded_by IS NULL
  AND (OLD.band IS DISTINCT FROM 'immediate')
)
EXECUTE FUNCTION public.call_fanout_assessment();

-- ---------------------------------------------------------------------------
-- 3) call_reactor_assetdoc() — webhook dispatcher for asset_documents.
-- Stream 3's Stage 10 + ingestion adapters insert into asset_documents.
-- Reactor receives the webhook, derives trigger_type, and enqueues
-- orchestrator_runs.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.call_reactor_assetdoc()
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
      'old_record', NULL
    ),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-supabase-webhook-secret', coalesce(webhook_secret, '')
    ),
    timeout_milliseconds := 30000
  );
  RETURN NEW;
END;
$function$;

COMMENT ON FUNCTION public.call_reactor_assetdoc() IS
  'v3 Stream 1: dispatch asset_documents AFTER INSERT to reactor edge function. Reactor enqueues an orchestrator_runs row (trigger_type=new_doc, possibly cross_source if a sibling primary doc exists in the 24h window). WHEN clause filters to material primary links only.';

DROP TRIGGER IF EXISTS asset_documents_primary_reactor_wh
  ON public.asset_documents;
CREATE TRIGGER asset_documents_primary_reactor_wh
AFTER INSERT ON public.asset_documents
FOR EACH ROW
WHEN (NEW.link_type = 'primary' AND NEW.is_material = true)
EXECUTE FUNCTION public.call_reactor_assetdoc();

-- ---------------------------------------------------------------------------
-- 4) orchestrator_runs dedupe — partial unique index so the reactor's
-- 10-min coalesce works as ON CONFLICT DO NOTHING.
-- One pending run per (asset_id, trigger_type, trigger_doc_id). Once the
-- drainer flips status to 'running'/'completed', the partial-unique
-- predicate releases — a follow-up doc on the same asset can enqueue a
-- new pending row.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS orchestrator_runs_pending_dedup_idx
  ON public.orchestrator_runs (asset_id, trigger_type, COALESCE(trigger_doc_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- 5) Notes
--
-- - The signals_insert_wh + signals_update_wh triggers are NOT touched. The
--   reactor edge function itself decides whether to run the legacy v2
--   convergence path (non-FDA profiles) or to enqueue orchestrator_runs (FDA
--   profiles) based on payload.record.scoring_profile.
-- - The new fanout trigger predicate uses NEW.band='immediate'. If a band
--   shift happens via UPDATE (e.g. ensemble re-run reclassifies to
--   'immediate'), the AFTER UPDATE companion trigger above catches it. If a
--   future re-stamp on the same row happens with band still 'immediate', no
--   new email fires (the dedup index on alert_deliveries blocks it within
--   the same day, and the band-flip predicate only matches transitions
--   INTO 'immediate' from another value).
-- - Rollback: DROP the two new triggers + functions + the
--   alert_deliveries.assessment_id column. v2 paths are unaffected.
-- ---------------------------------------------------------------------------
