-- ============================================================================
-- signal_entity_resolver: extend operator_flags.source CHECK
-- ============================================================================
-- The signal_entity_resolver Cowork skill (.claude/skills/signal_entity_resolver.md)
-- drains source='bridge_signal_to_v3' flags and seeds fda_assets. It needs two
-- new operator_flags sources:
--   * 'signal_entity_resolver_hard_halt' — operator kill-switch (step 0 check)
--   * 'signal_entity_resolver_run'        — per-run audit summary (step 6)
-- Idempotent: re-runnable, preserves the existing whitelist verbatim.
-- Disk-tracked DDL — apply via `supabase db push` (NOT MCP apply_migration).

DO $$
DECLARE
  v_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint
  WHERE conrelid = 'public.operator_flags'::regclass
    AND conname = 'operator_flags_source_check';

  IF v_def IS NULL THEN
    RAISE NOTICE 'operator_flags_source_check not found — skipping';
  ELSIF v_def LIKE '%signal_entity_resolver_run%' THEN
    RAISE NOTICE 'signal_entity_resolver sources already in operator_flags_source_check';
  ELSE
    ALTER TABLE public.operator_flags DROP CONSTRAINT operator_flags_source_check;
    ALTER TABLE public.operator_flags ADD CONSTRAINT operator_flags_source_check
      CHECK (source = ANY (ARRAY[
        'translation_health','scanner_probe','scanner_liveness','convergence_qa',
        'candidate_aging','thesis_writer','reactor','reporting_weekly',
        'litigation_baselines','edgar_runtime_health','scanner_failure_streak',
        'rollback_monitor','orchestrator_cost','thesis_jobs','manual',
        'v3_pipeline_watchdog','aging_review','challenger_retro',
        'constitutional_check','memory_writeback','tier2_quality',
        'orphan_sweeper','backfill_v3_assessment',
        'bridge_signal_to_v3',
        'signal_entity_resolver_hard_halt',
        'signal_entity_resolver_run'
      ]));
  END IF;
END $$;
