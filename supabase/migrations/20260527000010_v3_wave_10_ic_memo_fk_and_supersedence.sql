-- 20260527000010_v3_wave_10_ic_memo_fk_and_supersedence.sql
-- Wave 10 — wire IC memo into the assessment lifecycle.
--
-- Two changes:
--
-- 1. convergence_assessments.ic_memo_call_id
--    Forward reference to the sub_agent_calls row that holds the IC memo
--    synthesis for this assessment (role='ic_memo'). Today the only path
--    from an assessment to its memo is by joining via assessment_id +
--    role='ic_memo' filter, which: (a) returns multiple rows once we
--    refresh memos on supersedence, and (b) doesn't let the dashboard
--    surface "this assessment has a memo" without a separate query.
--
--    Nullable: most assessments will not have a memo. Wave 10.2 auto-fires
--    IC memo only when calibrated_conviction >= 75 AND band IN
--    ('immediate', 'watchlist').
--
--    FK is ON DELETE SET NULL: deleting a sub_agent_calls row (e.g. data
--    cleanup) leaves the assessment intact with a dangling pointer set to
--    NULL; the assessment row itself never depends on a memo for its own
--    correctness.
--
-- 2. sub_agent_calls.superseded_at
--    When a fresh assessment supersedes a prior one (sets
--    superseded_by/superseded_at on the old row), we also mark the prior
--    memo as superseded so the dashboard can surface "this memo is stale
--    — view the live one" without re-deriving the chain.
--
--    Stamped by runtime.stage_10_persist when it detects the parent
--    assessment had supersedence semantics.

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS ic_memo_call_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'convergence_assessments_ic_memo_call_id_fkey'
  ) THEN
    ALTER TABLE public.convergence_assessments
      ADD CONSTRAINT convergence_assessments_ic_memo_call_id_fkey
      FOREIGN KEY (ic_memo_call_id)
      REFERENCES public.sub_agent_calls(id)
      ON DELETE SET NULL;
  END IF;
END$$;

ALTER TABLE public.sub_agent_calls
  ADD COLUMN IF NOT EXISTS superseded_at timestamp with time zone;

-- Helper view: assessments with their (live) IC memo. Powers the dashboard
-- "Assessment + IC memo" join without callers needing to know the role
-- filter or the superseded_at semantics.
CREATE OR REPLACE VIEW public.v_assessment_with_ic_memo AS
SELECT
  ca.id                          AS assessment_id,
  ca.asset_id,
  ca.created_at                  AS assessment_at,
  ca.conviction_pct_calibrated,
  ca.band,
  ca.thesis_direction,
  ca.superseded_at               AS assessment_superseded_at,
  sac.id                         AS ic_memo_call_id,
  sac.output                     AS ic_memo_output,
  sac.created_at                 AS ic_memo_created_at,
  sac.superseded_at              AS ic_memo_superseded_at,
  sac.cost_usd                   AS ic_memo_cost_usd,
  sac.latency_ms                 AS ic_memo_latency_ms
FROM public.convergence_assessments ca
LEFT JOIN public.sub_agent_calls sac
  ON sac.id = ca.ic_memo_call_id;

COMMENT ON COLUMN public.convergence_assessments.ic_memo_call_id IS
  'Wave 10 — FK to sub_agent_calls(id) for the IC memo synthesis row '
  'attached to this assessment. NULL when no memo was triggered (most '
  'assessments). Auto-populated when calibrated conviction >= 75 AND '
  'band IN (immediate, watchlist).';

COMMENT ON COLUMN public.sub_agent_calls.superseded_at IS
  'Wave 10 — stamped when a new assessment for the same asset overtakes '
  'the assessment this memo is attached to. NULL = live. Dashboard '
  'surfaces non-NULL as "stale memo".';
