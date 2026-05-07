-- 20260510000010_v3_stream6_safety_and_cleanup.sql
-- Stream 6: production safety + small cleanups (D-122).
-- Bundles six related deltas, all additive/reversible:
--   1. Deprecate 15 non-FDA scanners (FDA-depth pivot per memory entry
--      strategic_pivot_fda_depth.md, 2026-05-06).
--   2. Add `tier` to dashboard_signal_rows view via LATERAL JOIN to the
--      latest-completed orchestrator_runs row per entity.
--   3. Extend fda_agent_reviews.agent_kind CHECK with 'literature',
--      'competitive', 'ic_memo' (sub-agents added in D-107).
--   4. Extend operator_flags.source CHECK with 'orchestrator_cost' (cost
--      ceiling soft-alert channel for Stream 6 cost enforcement).
--   5. Add pass2_verdict / pass2_confidence / pass2_at to asset_documents
--      and a partial index for the pass-2 backlog.
--   6. Extend orchestrator_runs.status CHECK with 'killed_budget' (mid-flight
--      hard kill — distinct from existing 'skipped_budget' which is
--      pre-flight skip).
--
-- Stream 6 step 4 (the actual ceiling enforcement code in client.py +
-- runtime.py) is deferred until Tier 0 (D-117..D-121) merges; this migration
-- only lays the schema.

-- ============================================================================
-- 1. Scanner deprecation (FDA-depth pivot)
-- ============================================================================

UPDATE public.scanners
   SET status = 'deprecated', updated_at = now()
 WHERE name IN (
   'asx_scanner','bse_nse_scanner','bmv_scanner','congressional_trading',
   'courtlistener_scanner','cvm_scanner','delaware_chancery_scanner',
   'esma_short_scanner','hkex_scanner','kind_scanner','lse_rns_scanner',
   'sec_enforcement_scanner','sedar_plus_scanner','takeover_candidate_scanner',
   'tdnet_scanner'
 );

-- ============================================================================
-- 2. dashboard_signal_rows.tier
-- ============================================================================
-- Adds the orchestrator tier (1=API SDK direct, 2=Cowork bulk, 3=Batch) to
-- the dashboard read model. Picks the latest *completed* orchestrator_runs
-- row across all fda_assets matching the signal's entity_id. This means an
-- entity with multiple assets exposes only the most recent tier — operators
-- who need per-asset tier should query orchestrator_runs directly.

CREATE OR REPLACE VIEW public.dashboard_signal_rows
WITH (security_invoker = true)
AS
SELECT
  s.signal_id,
  s.entity_id,
  s.issuer_figi,
  s.scanner_id,
  s.scanner_run_id,
  s.scoring_profile,
  s.rubric_version_id,
  s.source_content_hash,
  s.source_url,
  s.source_date,
  s.scan_date,
  s.signal_type,
  s.thesis_direction,
  s.strength_estimate,
  s.imported,
  s.dimensions,
  s.score,
  s.band,
  s.auto_caps_triggered,
  s.convergence_key,
  s.convergence_bonus,
  s.score_with_bonus,
  s.band_with_bonus,
  COALESCE(s.score_with_bonus, s.score) AS display_score,
  COALESCE(s.band_with_bonus, s.band) AS display_band,
  s.convergence_evaluated_at,
  s.raw_payload,
  s.extensions,
  s.created_at,
  e.name AS entity_name,
  e.primary_ticker,
  e.primary_mic,
  sc.name AS scanner_name,
  sc.geography AS scanner_geography,
  sc.cadence AS scanner_cadence,
  latest_run.tier AS tier
FROM public.signals s
LEFT JOIN public.entities e ON e.id = s.entity_id
LEFT JOIN public.scanners sc ON sc.id = s.scanner_id
LEFT JOIN LATERAL (
  SELECT orun.tier
    FROM public.orchestrator_runs orun
    JOIN public.fda_assets fa ON fa.id = orun.asset_id
   WHERE fa.entity_id = s.entity_id
     AND orun.status = 'completed'
   ORDER BY orun.completed_at DESC
   LIMIT 1
) latest_run ON true;

GRANT SELECT ON public.dashboard_signal_rows TO authenticated;

COMMENT ON COLUMN public.dashboard_signal_rows.tier IS
  'Orchestrator tier (1/2/3) of the latest completed orchestrator_runs row '
  'across all fda_assets matching this signal.entity_id. NULL if no completed '
  'run exists. Entities with multiple assets expose only the most recent run.';

-- ============================================================================
-- 3. fda_agent_reviews.agent_kind CHECK extension
-- ============================================================================

ALTER TABLE public.fda_agent_reviews
  DROP CONSTRAINT IF EXISTS fda_agent_reviews_agent_kind_check;

ALTER TABLE public.fda_agent_reviews
  ADD CONSTRAINT fda_agent_reviews_agent_kind_check
  CHECK (agent_kind IN (
    'medical','regulatory','microstructure',
    'literature','competitive','ic_memo'
  ));

-- Update the inline guard in fda_event_request_specialist_refresh RPC
-- (duplicate enum check that would otherwise reject the new kinds).
CREATE OR REPLACE FUNCTION public.fda_event_request_specialist_refresh(
  p_event_id uuid,
  p_agent_kind text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_review_id uuid;
  v_snapshot_hash text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: authentication required';
  END IF;
  IF p_agent_kind NOT IN (
    'medical','regulatory','microstructure',
    'literature','competitive','ic_memo'
  ) THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: agent_kind %, expected medical|regulatory|microstructure|literature|competitive|ic_memo', p_agent_kind;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.fda_regulatory_events WHERE id = p_event_id) THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: event % not found', p_event_id;
  END IF;

  v_snapshot_hash := 'manual:' || encode(gen_random_bytes(8), 'hex');

  INSERT INTO public.fda_agent_reviews (
    event_id, agent_kind, version, snapshot_hash, status
  )
  VALUES (
    p_event_id, p_agent_kind, 'pending', v_snapshot_hash, 'queued'
  )
  RETURNING id INTO v_review_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, payload
  )
  VALUES (
    v_actor,
    'fda_event_request_specialist_refresh',
    'fda_event',
    p_event_id::text,
    jsonb_build_object('agent_kind', p_agent_kind, 'review_id', v_review_id)
  );

  RETURN v_review_id;
END;
$$;

-- ============================================================================
-- 4. operator_flags.source CHECK extension
-- ============================================================================

ALTER TABLE public.operator_flags
  DROP CONSTRAINT IF EXISTS operator_flags_source_check;

ALTER TABLE public.operator_flags
  ADD CONSTRAINT operator_flags_source_check
  CHECK (source IN (
    'translation_health',
    'scanner_probe',
    'convergence_qa',
    'candidate_aging',
    'thesis_writer',
    'reactor',
    'reporting_weekly',
    'litigation_baselines',
    'edgar_runtime_health',
    'scanner_failure_streak',
    'rollback_monitor',
    'orchestrator_cost',
    'manual'
  ));

-- ============================================================================
-- 5. asset_documents pass-2 columns + backlog index
-- ============================================================================

ALTER TABLE public.asset_documents
  ADD COLUMN IF NOT EXISTS pass2_verdict text
    CHECK (pass2_verdict IS NULL OR pass2_verdict IN ('kept','demoted','rejected'));

ALTER TABLE public.asset_documents
  ADD COLUMN IF NOT EXISTS pass2_confidence numeric(3,2)
    CHECK (pass2_confidence IS NULL OR pass2_confidence BETWEEN 0 AND 1);

ALTER TABLE public.asset_documents
  ADD COLUMN IF NOT EXISTS pass2_at timestamptz;

-- Partial index for pass-2 backlog: only the rows pass-2 will visit.
CREATE INDEX IF NOT EXISTS asset_documents_pass2_pending_idx
  ON public.asset_documents(extraction_confidence)
  WHERE verified_by_pass2 = false
    AND extraction_method = 'agent_pass1';

COMMENT ON COLUMN public.asset_documents.pass2_verdict IS
  'Pass-2 (Haiku 4.5 verifier) verdict on pass-1 link: kept | demoted '
  '(real link but is_material=false) | rejected (spans do not substantiate). '
  'NULL until pass-2 has run.';

-- ============================================================================
-- 6. orchestrator_runs.status += 'killed_budget'
-- ============================================================================
-- 'skipped_budget' (already in enum) = pre-flight skip when budget exhausted
--   before the run starts.
-- 'killed_budget' (new) = mid-flight hard kill when per-run cost ceiling
--   ($15/run default) is breached during execution. Distinct so dashboards
--   can separate budget kills from genuine 'failed' runs.

ALTER TABLE public.orchestrator_runs
  DROP CONSTRAINT IF EXISTS orchestrator_runs_status_check;

ALTER TABLE public.orchestrator_runs
  ADD CONSTRAINT orchestrator_runs_status_check
  CHECK (status IN (
    'pending','running','completed','skipped_dedupe',
    'skipped_budget','killed_budget','failed'
  ));
