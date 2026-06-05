-- Restore the supersede-target guard to cleanup_orphaned_assessments().
--
-- ROOT CAUSE (operator_flags kind='live_patch_regression', fired 2026-06-02):
-- the live hand-patch `cleanup_orphaned_assessments_tier1_only` (applied via MCP
-- ~05-17/05-22, tracked by the Cowork patch-sentinel skill) excluded BOTH
-- Tier-2 rows AND superseded_by targets from orphan deletion. Three later
-- committed migrations redefined the function and the sentinel detected the
-- divergence:
--   20260528020000  simple all-tier delete (no guards)
--   20260601000040  tier IN (1,2) + rewire superseded_by refs before delete
--   20260602000010  Tier-2 marker design (CURRENT LIVE) — correct on Tier-2 but
--                    dropped ALL superseded_by handling.
-- The Tier-2 concern is now correctly handled by the `tier2_bulk_synthesis`
-- marker child written per emit (20260602000010), so the old tier=1 filter is
-- obsolete. But the superseded_by guard was lost:
-- convergence_assessments_superseded_by_fkey is ON DELETE NO ACTION, so if the
-- sweeper ever deletes a row that another row references via superseded_by, the
-- whole DELETE raises foreign_key_violation and the orphan-sweep (pg_cron job
-- 14, every 15 min) fails. Dormant today (0 at-risk rows) but a latent break.
--
-- FIX: keep the 20260602000010 Tier-2-marker design verbatim and re-add ONLY
-- the supersede-target exclusion — the hand-patch's documented intent ("exclude
-- supersede-target rows from orphan classification"). Do NOT run the sentinel's
-- recovery_sql: it would reinstate the obsolete tier=1 filter and drop the
-- tier_counts telemetry. After this lands, resolve the live_patch_regression
-- flag and retire the obsolete patch entry in the Cowork patch-sentinel.

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
  -- A real assessment must have at least one stage metric child. Tier-2 emits a
  -- single `tier2_bulk_synthesis` marker because it deliberately skips the
  -- Tier-1 stage graph. Rows with zero children after 15 minutes are invalid.
  -- Supersede-target rows (referenced by another row's superseded_by) are
  -- EXCLUDED: convergence_assessments_superseded_by_fkey is ON DELETE NO ACTION,
  -- so deleting one would raise foreign_key_violation and fail the whole sweep.
  WITH orphans AS (
    DELETE FROM public.convergence_assessments ca
    WHERE ca.created_at < now() - interval '15 minutes'
      AND NOT EXISTS (
        SELECT 1 FROM public.assessment_stage_metrics asm
        WHERE asm.assessment_id = ca.id
      )
      AND NOT EXISTS (
        SELECT 1 FROM public.convergence_assessments other
        WHERE other.superseded_by = ca.id
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
      'Orphan parent rows with zero assessment_stage_metrics children, older '
        || 'than 15 minutes, not referenced as a superseded_by target. Tier-2 '
        || 'rows should carry a tier2_bulk_synthesis marker; investigate any '
        || 'deleted tier=2 ids.',
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
  'Delete convergence_assessments older than 15 minutes with zero '
  'assessment_stage_metrics children, EXCLUDING rows referenced by another '
  'row''s superseded_by (FK is NO ACTION — deleting one would fail the sweep). '
  'Tier-2 emits one tier2_bulk_synthesis marker child, so valid Tier-2 rows '
  'satisfy the same invariant as Tier-1. Supersede guard restored 2026-06-05 '
  'after 20260602000010 dropped it (operator_flags live_patch_regression).';
