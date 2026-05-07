-- Phase 4B foundation — add `tier` column to convergence_assessments.
--
-- The orchestrator_runs queue table already carries a `tier` column
-- (1 API SDK direct, 2 Cowork bulk, 3 Batch backtest — see
-- 20260506000020_v3_phase_2_orchestrator_schema.sql line 22). The output
-- table convergence_assessments did not — Tier-1 vs Tier-2 rows were only
-- distinguishable by `orchestrator_version` (e.g. 'opus47_v3.1' vs 'bulk_v0').
-- That's brittle: any string-format change breaks tier-aware queries.
--
-- This migration adds a first-class `tier` column, defaulting to 1 so existing
-- rows preserve their semantics, with the same CHECK constraint as
-- orchestrator_runs.tier. It also adds a partial index on
-- (tier, asset_id, created_at DESC) for the Phase 4B dashboard / nightly
-- calibration refit (D-103 paired-bootstrap eval, per bulk_orchestrator.md
-- §Verification: tier-1 vs tier-2 Brier delta).

ALTER TABLE convergence_assessments
  ADD COLUMN IF NOT EXISTS tier int NOT NULL DEFAULT 1
    CHECK (tier IN (1, 2, 3));

-- For Phase 4B's nightly Brier-by-tier comparison and dashboard tier filters.
CREATE INDEX IF NOT EXISTS convergence_assessments_tier_asset_idx
  ON convergence_assessments(tier, asset_id, created_at DESC)
  WHERE superseded_at IS NULL;

COMMENT ON COLUMN convergence_assessments.tier IS
  'v3 execution tier: 1=API SDK direct (Tier-1, full pipeline ~$15/run); '
  '2=Cowork bulk (Tier-2 single Sonnet pass ~$0.50/run, see bulk_orchestrator skill); '
  '3=Batch backtest (Anthropic Batch API). Mirrors orchestrator_runs.tier. '
  'Phase 4B (2026-05-08).';
