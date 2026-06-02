-- IC memo backlog drainer + ongoing trigger.
-- Fires one ic_memo per eligible asset: immediate/watchlist band, the asset's
-- LATEST pending fda_regulatory_event has no ic_memo yet, assessment <30d old.
-- Picks the asset's latest such assessment (memo persists keyed to the asset's
-- latest pending event, so dedup is per-asset). Paced via pg_sleep; LIMITed.
-- Fire-and-forget: pg_net times out at 30s but Modal completes the ~58s inline
-- synthesis and persists. Verify success via the fda_agent_reviews row, NOT the
-- pg_net http_response. Idempotent gate => a persisted memo drops the asset out.
CREATE OR REPLACE FUNCTION public._ic_memo_backlog_tick(p_limit int DEFAULT 5)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public','extensions','pg_temp'
AS $$
DECLARE
  r record;
  n int := 0;
BEGIN
  FOR r IN
    WITH asset_latest_event AS (
      SELECT DISTINCT ON (e.asset_id) e.asset_id, e.id AS event_id
      FROM public.fda_regulatory_events e
      WHERE e.event_status = 'pending'
      ORDER BY e.asset_id, e.created_at DESC
    )
    SELECT DISTINCT ON (ca.asset_id) ca.id AS assessment_id
    FROM public.convergence_assessments ca
    JOIN asset_latest_event ale ON ale.asset_id = ca.asset_id
    WHERE ca.band IN ('immediate','watchlist')
      AND ca.created_at > now() - interval '30 days'
      AND NOT EXISTS (
        SELECT 1 FROM public.fda_agent_reviews fr
        WHERE fr.event_id = ale.event_id AND fr.agent_kind = 'ic_memo'
      )
    ORDER BY ca.asset_id, ca.created_at DESC
    LIMIT GREATEST(p_limit, 0)
  LOOP
    PERFORM public.rpc_ic_memo_run(r.assessment_id);
    n := n + 1;
    PERFORM pg_sleep(2);
  END LOOP;
  RETURN n;
END;
$$;
