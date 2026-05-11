-- F-312 affordances: per-(event_id, agent_kind) advisory-lock helper +
-- agent_review_drain_metrics view.
--
-- Background
-- ----------
-- The Cowork drain skill that processes queued fda_agent_reviews lives
-- outside this repo, so this migration can't change the drain implementation
-- itself. What it CAN do:
--
--   1. Provide a database-side advisory-lock primitive so the drain skill
--      can run multiple specialist agents (medical / regulatory /
--      microstructure) concurrently per event_id without contending on the
--      same fda_agent_reviews row.
--
--   2. Expose a metrics view (agent_review_drain_metrics) so Pedro can
--      observe drain rate empirically and decide whether parallelism is
--      worth building. Surfaced via the Conan dashboard.
--
-- Advisory lock contract
-- ----------------------
-- public.fda_agent_review_try_claim(p_review_id uuid) RETURNS bool
--   Acquires a transaction-level advisory lock keyed by hashing the review
--   id. Returns true if the caller now holds the lock (and should proceed
--   to update status='running' / 'completed'), false if another worker
--   already holds it (caller should skip / retry later).
--   The lock auto-releases on transaction end, so a crashed worker doesn't
--   block recovery.
--
-- Metrics view
-- ------------
-- public.agent_review_drain_metrics aggregates last-24-hour drain rate per
-- agent_kind:
--   * queue_depth  — current count of queued reviews
--   * drained_24h  — completions in the last 24h
--   * failed_24h   — failures in the last 24h
--   * p50_latency_seconds  — median wall-clock from enqueue (created_at) to
--                            completion (ran_at)
--   * p95_latency_seconds  — 95th percentile of same
--   * oldest_queued_age_seconds — how stale the queue tail is
--
-- Rollback
--   DROP VIEW IF EXISTS public.agent_review_drain_metrics;
--   DROP FUNCTION IF EXISTS public.fda_agent_review_try_claim(uuid);

CREATE OR REPLACE FUNCTION public.fda_agent_review_try_claim(p_review_id uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_key bigint;
BEGIN
  -- hashtextextended(uuid::text, seed) → stable 64-bit hash. Different seed
  -- from any other advisory-lock domain in the codebase to avoid collision
  -- (orchestrator drainer uses seed 0, this uses seed 1).
  v_key := hashtextextended(p_review_id::text, 1);
  RETURN pg_try_advisory_xact_lock(v_key);
END;
$func$;

COMMENT ON FUNCTION public.fda_agent_review_try_claim(uuid) IS
  'F-312: transaction-scoped advisory lock per fda_agent_reviews row id. '
  'Returns true if the caller acquired the lock (proceed to update row), '
  'false if another worker already holds it. Auto-releases at transaction '
  'end. Lets the Cowork drain skill run medical/regulatory/microstructure '
  'concurrently per event without SELECT FOR UPDATE contention.';

-- ----------------------------------------------------------------------
-- Drain metrics view
-- ----------------------------------------------------------------------

CREATE OR REPLACE VIEW public.agent_review_drain_metrics AS
WITH base AS (
  SELECT
    agent_kind,
    status,
    created_at,
    ran_at,
    CASE
      WHEN status = 'completed' AND ran_at IS NOT NULL
      THEN EXTRACT(EPOCH FROM (ran_at - created_at))
      ELSE NULL
    END AS latency_seconds
  FROM public.fda_agent_reviews
  WHERE created_at > NOW() - INTERVAL '7 days'
),
per_kind AS (
  SELECT
    agent_kind,
    COUNT(*) FILTER (WHERE status = 'queued') AS queue_depth,
    COUNT(*) FILTER (WHERE status = 'completed' AND ran_at > NOW() - INTERVAL '24 hours') AS drained_24h,
    COUNT(*) FILTER (WHERE status = 'failed'    AND ran_at > NOW() - INTERVAL '24 hours') AS failed_24h,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_seconds)
      FILTER (WHERE latency_seconds IS NOT NULL AND ran_at > NOW() - INTERVAL '24 hours')
      AS p50_latency_seconds,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_seconds)
      FILTER (WHERE latency_seconds IS NOT NULL AND ran_at > NOW() - INTERVAL '24 hours')
      AS p95_latency_seconds,
    MAX(EXTRACT(EPOCH FROM (NOW() - created_at)))
      FILTER (WHERE status = 'queued')
      AS oldest_queued_age_seconds
  FROM base
  GROUP BY agent_kind
)
SELECT * FROM per_kind
ORDER BY agent_kind;

COMMENT ON VIEW public.agent_review_drain_metrics IS
  'F-312: per-agent-kind drain metrics (queue depth + 24h completions + p50/p95 latency). '
  'Drives the operator decision on whether to add Modal-based parallel drain.';
