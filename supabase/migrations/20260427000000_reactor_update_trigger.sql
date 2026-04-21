-- Wire signals.UPDATE → reactor webhook so signal_resolver's NULL→non-NULL score
-- transitions (and provisional-heuristic → ai_resolved transitions) actually
-- re-run convergence. Without this trigger, resolver-scored rows land with
-- convergence_key / convergence_evaluated_at / band_with_bonus / score_with_bonus
-- all NULL, because reactor/index.ts's UPDATE path was written but never wired
-- from the SQL side (only an AFTER INSERT trigger existed).
--
-- Two parts:
--   (1) call_reactor() — populate old_record with row_to_json(OLD) on UPDATE so
--       shouldProcessUpdate() in reactor/scoring-state.ts can inspect the
--       previous row and decide whether to process. INSERT stays old_record=null.
--   (2) signals_update_wh — AFTER UPDATE trigger, gated at SQL level to only fire
--       when score or dimensions._provenance changes. stampRow() and
--       clearDisplacedWinners() both patch only convergence_* / *_with_bonus
--       columns, so the WHEN clause filters those out and prevents self-retrigger.
--       The edge function's shouldProcessUpdate() does a second-pass filter.

CREATE OR REPLACE FUNCTION public.call_reactor()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
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
      'old_record', CASE WHEN TG_OP = 'UPDATE' THEN row_to_json(OLD) ELSE NULL END
    ),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-supabase-webhook-secret', coalesce(webhook_secret, '')
    ),
    timeout_milliseconds := 30000
  );
  RETURN NEW;
END;
$$;

CREATE TRIGGER signals_update_wh
AFTER UPDATE ON public.signals
FOR EACH ROW
WHEN (
  OLD.score IS DISTINCT FROM NEW.score
  OR (OLD.dimensions->>'_provenance') IS DISTINCT FROM (NEW.dimensions->>'_provenance')
)
EXECUTE FUNCTION public.call_reactor();
