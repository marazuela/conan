-- 20260524000040_v3_fda_aging_sql_functions.sql
-- Three SQL functions that drive the v3 aging methodology port:
--   1. v3_fda_aging_stage_a()           — daily deterministic 5-rule sweep
--   2. v3_prior_failure_guard(asset)    — §8a re-draft block before orchestrator dispatch
--   3. v3_challenger_retro_sql_kernel() — weekly stratified sample for challenger_retro replay
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M5)

-- ============================================================================
-- Schema prerequisite: aging_state_since on fda_assets
-- ============================================================================
-- Tracks when the asset entered its current aging_state. The Stage A
-- "watch >60d" / "stale active >30d" rules read this to avoid being fooled
-- by daily last_aging_evaluated_at updates. Backfill from created_at so the
-- column has realistic values for pre-existing assets on day 1.

ALTER TABLE public.fda_assets
  ADD COLUMN IF NOT EXISTS aging_state_since timestamptz NOT NULL DEFAULT now();

UPDATE public.fda_assets
   SET aging_state_since = created_at
 WHERE aging_state_since >= now() - interval '5 minutes'
   AND created_at < now() - interval '5 minutes';

COMMENT ON COLUMN public.fda_assets.aging_state_since IS
  'v3 aging: timestamp when the asset entered its current aging_state. '
  'Updated by v3_fda_aging_stage_a() on every state transition. Read by '
  'the >60d watch-aged-out rule and the >30d stale-active rule. Backfilled '
  'from created_at on M5 apply so day-1 evaluations have realistic ages.';

-- ============================================================================
-- 1. v3_fda_aging_stage_a() — deterministic 5-rule sweep
-- ============================================================================
-- Daily 06:00 UTC. Single transaction. Precedence (highest first):
--   1. aged_out_no_catalyst       (watch >60d, no near catalyst)   → kill
--   2. catalyst_elapsed_gt_7d     (active, catalyst >7d past)      → demote + supersede
--   3. stale_active_no_catalyst   (active >30d, no near catalyst)  → demote
--   4. promote_catalyst_within_60d(watch, catalyst in next 60d,
--                                  routine_declined IS NOT true)   → promote
--   5. catalyst_just_elapsed      (active, catalyst 1–7d past)     → flag_for_review (kill_pending)
-- Other assets get last_aging_evaluated_at updated; no verdict row written
-- for them (keeps fda_aging_verdicts sparse).
-- Returns count of assets that had a state transition.

CREATE OR REPLACE FUNCTION public.v3_fda_aging_stage_a()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_now timestamptz := now();
  v_touched int := 0;
BEGIN
  -- Classify every active asset due for evaluation today.
  WITH due AS (
    SELECT fa.id,
           fa.aging_state,
           fa.aging_state_since,
           fa.next_catalyst_date,
           fa.aging_extensions
      FROM public.fda_assets fa
     WHERE fa.is_active = true
       AND (fa.last_aging_evaluated_at IS NULL
            OR fa.last_aging_evaluated_at::date < current_date)
  ),
  classified AS (
    SELECT id,
           aging_state,
           CASE
             WHEN aging_state = 'watch'
                  AND aging_state_since < v_now - interval '60 days'
                  AND (next_catalyst_date IS NULL
                       OR next_catalyst_date > current_date + 60)
               THEN 'aged_out_no_catalyst'
             WHEN aging_state = 'active'
                  AND next_catalyst_date IS NOT NULL
                  AND next_catalyst_date < current_date - 7
               THEN 'catalyst_elapsed_gt_7d'
             WHEN aging_state = 'active'
                  AND aging_state_since < v_now - interval '30 days'
                  AND (next_catalyst_date IS NULL
                       OR next_catalyst_date > current_date + 60)
               THEN 'stale_active_no_catalyst'
             WHEN aging_state = 'watch'
                  AND next_catalyst_date IS NOT NULL
                  AND next_catalyst_date BETWEEN current_date AND current_date + 60
                  AND (aging_extensions->>'routine_declined' IS NULL
                       OR aging_extensions->>'routine_declined' IS DISTINCT FROM 'true')
               THEN 'promote_catalyst_within_60d'
             WHEN aging_state = 'active'
                  AND next_catalyst_date IS NOT NULL
                  AND next_catalyst_date BETWEEN current_date - 7 AND current_date - 1
               THEN 'catalyst_just_elapsed'
             ELSE 'maintain'
           END AS rule_id
      FROM due
  ),
  transitions AS (
    SELECT id, rule_id,
           CASE rule_id
             WHEN 'aged_out_no_catalyst'         THEN 'expired'
             WHEN 'catalyst_elapsed_gt_7d'       THEN 'demoted'
             WHEN 'stale_active_no_catalyst'    THEN 'demoted'
             WHEN 'promote_catalyst_within_60d' THEN 'active'
             WHEN 'catalyst_just_elapsed'       THEN 'kill_pending'
           END AS new_state,
           CASE rule_id
             WHEN 'aged_out_no_catalyst'         THEN 'kill'
             WHEN 'catalyst_elapsed_gt_7d'       THEN 'demote_to_watch'
             WHEN 'stale_active_no_catalyst'    THEN 'demote_to_watch'
             WHEN 'promote_catalyst_within_60d' THEN 'promote_to_active'
             WHEN 'catalyst_just_elapsed'       THEN 'flag_for_review'
           END AS recommendation
      FROM classified
     WHERE rule_id <> 'maintain'
  ),
  ins_verdicts AS (
    INSERT INTO public.fda_aging_verdicts
      (asset_id, evaluated_at, stage, recommendation, trigger_rule)
    SELECT id, v_now, 'a_deterministic', recommendation, rule_id
      FROM transitions
    RETURNING asset_id
  ),
  upd_state AS (
    UPDATE public.fda_assets fa
       SET aging_state          = t.new_state,
           aging_state_since    = v_now,
           is_active            = CASE
                                    WHEN t.new_state = 'expired' THEN false
                                    ELSE fa.is_active
                                  END,
           last_aging_evaluated_at = v_now
      FROM transitions t
     WHERE fa.id = t.id
    RETURNING fa.id
  ),
  -- Supersede the latest non-superseded convergence_assessment for assets
  -- whose catalyst elapsed >7d (forces re-assessment on next orchestrator run).
  supersede AS (
    UPDATE public.convergence_assessments ca
       SET superseded_at = v_now
      FROM transitions t
     WHERE t.rule_id = 'catalyst_elapsed_gt_7d'
       AND ca.asset_id = t.id
       AND ca.superseded_at IS NULL
       AND ca.id = (
         SELECT ca2.id
           FROM public.convergence_assessments ca2
          WHERE ca2.asset_id = t.id
            AND ca2.superseded_at IS NULL
          ORDER BY ca2.created_at DESC
          LIMIT 1
       )
    RETURNING ca.id
  )
  -- Update last_aging_evaluated_at for non-transitioning assets too, so they
  -- don't re-classify tomorrow. (Transitioning assets already updated above.)
  UPDATE public.fda_assets fa
     SET last_aging_evaluated_at = v_now
    FROM classified c
   WHERE c.rule_id = 'maintain'
     AND fa.id = c.id;

  SELECT count(*) INTO v_touched FROM public.fda_aging_verdicts
   WHERE stage = 'a_deterministic'
     AND evaluated_at = v_now;

  RETURN v_touched;
END;
$$;

COMMENT ON FUNCTION public.v3_fda_aging_stage_a() IS
  'v3 daily Stage A aging sweep. Deterministic 5-rule classification of active '
  'fda_assets. Returns count of assets that had a state transition. Safe to '
  'BEGIN; SELECT v3_fda_aging_stage_a(); ROLLBACK; for dry-run verification.';

-- ============================================================================
-- 2. v3_prior_failure_guard(asset_id) — §8a re-draft block
-- ============================================================================
-- Returns FALSE (block dispatch) when the asset has an open kill verdict in
-- the last 24h whose challenger_verdict='kill' AND aging_extensions carries
-- routine_declined='true'. Returns TRUE otherwise. Called by
-- orchestrator_drain_queue pre-dispatch in modal_workers/orchestrator_app.py.

CREATE OR REPLACE FUNCTION public.v3_prior_failure_guard(p_asset_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT NOT EXISTS (
    SELECT 1
      FROM public.fda_aging_verdicts v
      JOIN public.fda_assets fa ON fa.id = v.asset_id
     WHERE v.asset_id = p_asset_id
       AND v.recommendation = 'kill'
       AND v.challenger_verdict = 'kill'
       AND fa.aging_extensions->>'routine_declined' = 'true'
       AND v.created_at > now() - interval '24 hours'
  );
$$;

COMMENT ON FUNCTION public.v3_prior_failure_guard(uuid) IS
  'v3 §8a guard. Mirrors v2 thesis_writer prior-failure resolution: an asset '
  'recently flagged kill by both the Stage B recommendation AND the challenger '
  'verdict (with routine_declined sticky-set) should not be re-dispatched '
  'within 24h. The flag is cleared by the next passing Stage B promotion '
  'on this asset.';

-- ============================================================================
-- 3. v3_challenger_retro_sql_kernel(window_days) — stratified sample
-- ============================================================================
-- Weekly Sun 09:00 UTC. Returns a 10-row stratified sample of resolved
-- convergence_assessments + universe-size-based tier flag. The Cowork skill
-- fda_challenger_replay reads this, replays Stage 3 pre-mortem on each row,
-- classifies (new_verdict × realized_outcome) per the v2 11-row matrix, and
-- writes one accuracy_metrics row with auditor='challenger_retro'.
--
-- Sample shape: 3 pre_edge_hit + 3 dead_catalyst + 2 post_edge_miss + 2 wildcards.
-- Tier:
--   'full'         — universe ≥5 pre_edge_hit AND ≥5 dead_catalyst (rate flags allowed)
--   'preview'      — universe ≥3 in either bucket (metrics row only, no flags)
--   'insufficient' — below preview (zero-row metrics; skip the replay)
--
-- Outcome labels are pulled from post_mortem_queue.realized_outcome->>'label'.
-- That key is set by post_mortem_runner when the window resolves; v2 used
-- outcomes.outcome_label which is now in archive_v2.

CREATE OR REPLACE FUNCTION public.v3_challenger_retro_sql_kernel(p_window_days int DEFAULT 90)
RETURNS TABLE (
  assessment_id uuid,
  asset_id uuid,
  stratum text,
  predicted_outcome text,
  predicted_conviction_pct numeric,
  predicted_direction text,
  realized_outcome jsonb,
  pre_mortem_verdict text,
  hypothesis_count int,
  tier text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_pre_edge_hit_universe int;
  v_dead_catalyst_universe int;
  v_tier text;
BEGIN
  SELECT count(*) INTO v_pre_edge_hit_universe
    FROM public.convergence_assessments ca
    JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
   WHERE pmq.status = 'post_mortem_complete'
     AND ca.created_at > now() - (p_window_days || ' days')::interval
     AND pmq.realized_outcome->>'label' = 'pre_edge_hit';

  SELECT count(*) INTO v_dead_catalyst_universe
    FROM public.convergence_assessments ca
    JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
   WHERE pmq.status = 'post_mortem_complete'
     AND ca.created_at > now() - (p_window_days || ' days')::interval
     AND pmq.realized_outcome->>'label' = 'dead_catalyst';

  v_tier := CASE
              WHEN v_pre_edge_hit_universe >= 5
                   AND v_dead_catalyst_universe >= 5 THEN 'full'
              WHEN v_pre_edge_hit_universe >= 3
                   OR v_dead_catalyst_universe >= 3  THEN 'preview'
              ELSE 'insufficient'
            END;

  RETURN QUERY
  WITH sample AS (
    (
      SELECT ca.id   AS aid,
             ca.asset_id AS asid,
             'pre_edge_hit'::text AS stratum,
             pmq.predicted_outcome,
             pmq.predicted_conviction_pct,
             pmq.predicted_direction,
             pmq.realized_outcome,
             ca.pre_mortem_verdict
        FROM public.convergence_assessments ca
        JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
       WHERE pmq.status = 'post_mortem_complete'
         AND ca.created_at > now() - (p_window_days || ' days')::interval
         AND pmq.realized_outcome->>'label' = 'pre_edge_hit'
       ORDER BY random()
       LIMIT 3
    )
    UNION ALL
    (
      SELECT ca.id, ca.asset_id, 'dead_catalyst'::text,
             pmq.predicted_outcome, pmq.predicted_conviction_pct, pmq.predicted_direction,
             pmq.realized_outcome, ca.pre_mortem_verdict
        FROM public.convergence_assessments ca
        JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
       WHERE pmq.status = 'post_mortem_complete'
         AND ca.created_at > now() - (p_window_days || ' days')::interval
         AND pmq.realized_outcome->>'label' = 'dead_catalyst'
       ORDER BY random()
       LIMIT 3
    )
    UNION ALL
    (
      SELECT ca.id, ca.asset_id, 'post_edge_miss'::text,
             pmq.predicted_outcome, pmq.predicted_conviction_pct, pmq.predicted_direction,
             pmq.realized_outcome, ca.pre_mortem_verdict
        FROM public.convergence_assessments ca
        JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
       WHERE pmq.status = 'post_mortem_complete'
         AND ca.created_at > now() - (p_window_days || ' days')::interval
         AND pmq.realized_outcome->>'label' = 'post_edge_miss'
       ORDER BY random()
       LIMIT 2
    )
    UNION ALL
    (
      SELECT ca.id, ca.asset_id, 'wildcard'::text,
             pmq.predicted_outcome, pmq.predicted_conviction_pct, pmq.predicted_direction,
             pmq.realized_outcome, ca.pre_mortem_verdict
        FROM public.convergence_assessments ca
        JOIN public.post_mortem_queue pmq ON pmq.assessment_id = ca.id
       WHERE pmq.status = 'post_mortem_complete'
         AND ca.created_at > now() - (p_window_days || ' days')::interval
         AND COALESCE(pmq.realized_outcome->>'label','') NOT IN
             ('pre_edge_hit','dead_catalyst','post_edge_miss')
       ORDER BY random()
       LIMIT 2
    )
  )
  SELECT
    s.aid,
    s.asid,
    s.stratum,
    s.predicted_outcome,
    s.predicted_conviction_pct,
    s.predicted_direction,
    s.realized_outcome,
    s.pre_mortem_verdict,
    (SELECT count(*)::int
       FROM public.hypothesis_enumeration he
      WHERE he.assessment_id = s.aid) AS hypothesis_count,
    v_tier
    FROM sample s;
END;
$$;

COMMENT ON FUNCTION public.v3_challenger_retro_sql_kernel(int) IS
  'v3 weekly challenger retrospective sampler. Returns a 10-row stratified '
  'sample (3 pre_edge_hit + 3 dead_catalyst + 2 post_edge_miss + 2 wildcard) '
  'of resolved assessments from the last N days. Tier flag (full/preview/'
  'insufficient) governs whether the Cowork skill is permitted to fire '
  'per-run operator_flags. Outcome labels read from realized_outcome->>label.';
