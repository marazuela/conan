-- Auto-refresh IC memos when the underlying assessment changes.
-- (1) supersedence: keep ONE active memo per event (latest), preserve history.
-- (2) refresh-aware drainer gate: fire when the asset's LATEST assessment has no
--     ACTIVE memo built from it (snapshot_hash = 'assessment:<assessment_id>').

-- (1a) additive nullable column — NULL = active (current) memo.
ALTER TABLE public.fda_agent_reviews
  ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

-- (1b) on a new completed ic_memo, supersede prior active ic_memo for the event.
CREATE OR REPLACE FUNCTION public._ic_memo_supersede_prior()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE public.fda_agent_reviews
     SET superseded_at = now()
   WHERE event_id = NEW.event_id
     AND agent_kind = 'ic_memo'
     AND id <> NEW.id
     AND superseded_at IS NULL;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ic_memo_supersede_prior ON public.fda_agent_reviews;
CREATE TRIGGER ic_memo_supersede_prior
  AFTER INSERT ON public.fda_agent_reviews
  FOR EACH ROW
  WHEN (NEW.agent_kind = 'ic_memo' AND NEW.status = 'completed')
  EXECUTE FUNCTION public._ic_memo_supersede_prior();

-- (2) refresh-aware drainer. Picks the latest immediate/watchlist <30d assessment
-- per asset, fires if NO active memo was built from THAT assessment. Initial:
-- fires (no memo). Refresh: a newer assessment => no active memo from it => fires
-- once; the new memo's snapshot_hash then matches => idle until the next new
-- assessment. Per-asset dedup; paced; LIMITed.
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
    ),
    latest_assessment AS (
      SELECT DISTINCT ON (ca.asset_id) ca.id AS assessment_id, ca.asset_id
      FROM public.convergence_assessments ca
      WHERE ca.band IN ('immediate','watchlist')
        AND ca.created_at > now() - interval '30 days'
      ORDER BY ca.asset_id, ca.created_at DESC
    )
    SELECT la.assessment_id
    FROM latest_assessment la
    JOIN asset_latest_event ale ON ale.asset_id = la.asset_id
    WHERE NOT EXISTS (
      SELECT 1 FROM public.fda_agent_reviews fr
      WHERE fr.event_id = ale.event_id
        AND fr.agent_kind = 'ic_memo'
        AND fr.status = 'completed'
        AND fr.superseded_at IS NULL
        AND fr.snapshot_hash = 'assessment:' || la.assessment_id::text
    )
    ORDER BY la.assessment_id
    LIMIT GREATEST(p_limit, 0)
  LOOP
    PERFORM public.rpc_ic_memo_run(r.assessment_id);
    n := n + 1;
    PERFORM pg_sleep(2);
  END LOOP;
  RETURN n;
END;
$$;
