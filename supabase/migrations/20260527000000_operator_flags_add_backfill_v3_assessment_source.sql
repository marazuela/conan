-- 20260527000000_operator_flags_add_backfill_v3_assessment_source.sql
-- Add 'backfill_v3_assessment' to operator_flags.source CHECK whitelist.
--
-- Why: modal_workers/scripts/backfill_v3_assessments.py emits per-asset
-- operator_flags (severity=info for successful asset_documents → reactor
-- enqueues, severity=warn when no representative document exists and the
-- asset needs human curation). Without this whitelist entry the INSERT
-- raises 23514.

-- NB: the live DB constraint as of 2026-05-14 already extends past the most
-- recent committed migration (20260526) with 'memory_writeback', 'tier2_quality',
-- and 'orphan_sweeper' — those were added out-of-band. This migration
-- preserves all live values and appends 'backfill_v3_assessment'.

ALTER TABLE public.operator_flags
  DROP CONSTRAINT IF EXISTS operator_flags_source_check;

ALTER TABLE public.operator_flags
  ADD CONSTRAINT operator_flags_source_check
  CHECK (source IN (
    'translation_health',
    'scanner_probe',
    'scanner_liveness',
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
    'thesis_jobs',
    'manual',
    'v3_pipeline_watchdog',
    'aging_review',
    'challenger_retro',
    'constitutional_check',
    'memory_writeback',
    'tier2_quality',
    'orphan_sweeper',
    'backfill_v3_assessment'
  ));
