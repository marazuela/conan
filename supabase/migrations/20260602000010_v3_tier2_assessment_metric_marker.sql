-- Phase 4B closeout: Tier-2 assessment metric marker.
--
-- The orphan sweeper treats "zero assessment_stage_metrics children after
-- 15 minutes" as the signal for an invalid convergence_assessments parent.
-- Tier-2 intentionally skips the Tier-1 stage graph, so the runtime now writes
-- one compact marker row (`tier2_bulk_synthesis`) per successful Tier-2 emit.
--
-- This migration backfills that marker for any existing Tier-2 rows and
-- refreshes cleanup_orphaned_assessments() comments/evidence so the invariant
-- is explicit in schema history.

BEGIN;

INSERT INTO public.assessment_stage_metrics (
  assessment_id,
  stage_name,
  model,
  input_tokens,
  output_tokens,
  thinking_tokens,
  cache_read_tokens,
  cache_creation_tokens,
  cost_usd,
  latency_ms,
  status,
  notes
)
SELECT
  ca.id,
  'tier2_bulk_synthesis',
  COALESCE(ca.model_id, 'claude-sonnet-4-6'),
  COALESCE(ca.total_input_tokens, 0),
  COALESCE(ca.total_output_tokens, 0),
  COALESCE(ca.total_thinking_tokens, 0),
  COALESCE(ca.total_cache_read_tokens, 0),
  COALESCE(ca.total_cache_creation_tokens, 0),
  COALESCE(ca.cost_usd, 0),
  COALESCE(ca.latency_ms, 0),
  'completed',
  jsonb_build_object(
    'tier', 2,
    'orchestrator_version', ca.orchestrator_version,
    'trigger_type', ca.trigger_type,
    'trigger_doc_id', ca.trigger_doc_id,
    'document_count', COALESCE(array_length(ca.document_ids, 1), 0),
    'fact_count', COALESCE(array_length(ca.fact_ids, 1), 0),
    'gate_status', ca.gate_status,
    'backfilled_by', '20260602000010_v3_tier2_assessment_metric_marker'
  )
FROM public.convergence_assessments ca
WHERE ca.tier = 2
  AND NOT EXISTS (
    SELECT 1
    FROM public.assessment_stage_metrics asm
    WHERE asm.assessment_id = ca.id
  );

CREATE OR REPLACE FUNCTION public.cleanup_orphaned_assessments()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_deleted_ids uuid[];
  v_deleted_tier_counts jsonb;
  v_count int;
BEGIN
  -- A real assessment must have at least one stage metric child. Tier-2 emits
  -- a single `tier2_bulk_synthesis` marker because it deliberately skips the
  -- Tier-1 stage graph. Rows with zero children after 15 minutes are still
  -- invalid and safe to delete.
  WITH orphans AS (
    DELETE FROM public.convergence_assessments ca
    WHERE ca.created_at < now() - interval '15 minutes'
      AND NOT EXISTS (
        SELECT 1 FROM public.assessment_stage_metrics asm
        WHERE asm.assessment_id = ca.id
      )
    RETURNING ca.id, ca.tier
  ),
  tier_counts AS (
    SELECT tier, count(*) AS n
    FROM orphans
    GROUP BY tier
  )
  SELECT
    (SELECT array_agg(id) FROM orphans),
    COALESCE(
      (SELECT jsonb_object_agg(tier::text, n) FROM tier_counts),
      '{}'::jsonb
    )
  INTO v_deleted_ids, v_deleted_tier_counts;

  v_count := COALESCE(array_length(v_deleted_ids, 1), 0);

  IF v_count > 0 THEN
    INSERT INTO public.operator_flags (
      severity, source, kind, title, body, evidence
    ) VALUES (
      'warn',
      'orphan_sweeper',
      'convergence_orphan_deleted',
      format('Cleaned up %s orphan convergence_assessments row(s)', v_count),
      'Orphan parent rows with zero assessment_stage_metrics children, '
        || 'older than 15 minutes. Tier-2 rows should carry a '
        || 'tier2_bulk_synthesis marker; investigate any deleted tier=2 ids.',
      jsonb_build_object(
        'deleted_ids', to_jsonb(v_deleted_ids),
        'deleted_count', v_count,
        'deleted_tier_counts', v_deleted_tier_counts,
        'sweeper_run_at', to_jsonb(now())
      )
    );
  END IF;

  RETURN v_count;
END;
$$;

COMMENT ON FUNCTION public.cleanup_orphaned_assessments() IS
  'Delete convergence_assessments rows older than 15 minutes with zero '
  'assessment_stage_metrics children. Tier-2 emits one tier2_bulk_synthesis '
  'marker child, so valid Tier-2 rows satisfy the same invariant as Tier-1.';

COMMIT;
