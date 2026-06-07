-- =============================================================================
-- 20260620000000_operator_flags_bc_sources.sql
--
-- Extends the shared v4 table public.operator_flags.source CHECK with the bc_
-- monitor alert sources so the Light-v4 monitor/digest/outcome workers can raise
-- operator flags. Until this lands, ANY bc_ operator_flags INSERT is rejected with
-- 23514 check_violation — the workers PRE-FLIGHT this CHECK and fall back to
-- bc_pipeline_runs.log, so they never crash for a flag-sink gap, but observability
-- is degraded and the fail-loud alerting story is incomplete.
--
-- Conan-ledger port of bc-fda/db/migrations/007_operator_flags_bc_sources.sql
-- (split-the-difference repo strategy: all bc DB objects own a single ledger here).
--
-- The 29 existing v4 sources below were RE-INTROSPECTED live on 2026-06-07 and match
-- the constraint exactly (zero drift). The 11 bc_ additions:
--   bc_l1_feature_builder   — Phase-1 feature-builder anomaly (refusal spike, etc.)
--   bc_l2_refusal_spike     — Phase-1 abnormal refusal rate
--   bc_event_cap            — Phase-2 per-name event cap hit (warn)
--   bc_daily_budget         — Phase-2 $5/day hard-kill tripped (critical)
--   bc_synthesis_failed     — Phase-2 synthesis schema/implausible/api failure
--   bc_l4_synthesis_failed / bc_l4_budget_exceeded / bc_cowork_stale /
--   bc_feature_drift / bc_calibration_drift / bc_synthesis_quality_decay
--     — forward-compatible superset (harmless if unused; bc_cowork_stale is the
--       P3 freshness guard for the Cowork-hosted synthesis step).
--
-- IDEMPOTENT: DROP CONSTRAINT IF EXISTS, then ADD. Re-running re-asserts the same
-- 40-value superset (a no-op). Wrapped so a missing operator_flags table is a clean
-- skip, not a hard error.
--
-- ⚠️ operator_flags is SHARED with Conan v4 — if the live CHECK drifts (a new v4
-- source added after 2026-06-07), RE-INTROSPECT and merge before applying so no
-- existing value is dropped:
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid='public.operator_flags'::regclass AND conname='operator_flags_source_check';
-- =============================================================================

DO $$
BEGIN
  IF to_regclass('public.operator_flags') IS NULL THEN
    RAISE NOTICE 'operator_flags table absent — skipping bc_ source CHECK extension.';
    RETURN;
  END IF;

  ALTER TABLE public.operator_flags DROP CONSTRAINT IF EXISTS operator_flags_source_check;

  ALTER TABLE public.operator_flags ADD CONSTRAINT operator_flags_source_check
    CHECK (source = ANY (ARRAY[
      -- ── existing v4 values (re-introspected live 2026-06-07) ──
      'translation_health','scanner_probe','scanner_liveness','convergence_qa','candidate_aging',
      'thesis_writer','reactor','reporting_weekly','litigation_baselines','edgar_runtime_health',
      'scanner_failure_streak','rollback_monitor','orchestrator_cost','thesis_jobs','manual',
      'v3_pipeline_watchdog','aging_review','challenger_retro','constitutional_check','memory_writeback',
      'tier2_quality','orphan_sweeper','backfill_v3_assessment','bridge_signal_to_v3',
      'signal_entity_resolver_hard_halt','signal_entity_resolver_run','asset_linker_hard_halt',
      'skill_watchdog','compute_circuit_breaker',
      -- ── bc_ monitor sources (this migration) ──
      'bc_l1_feature_builder','bc_l2_refusal_spike','bc_event_cap','bc_daily_budget','bc_synthesis_failed',
      -- ── forward-compatible bc_ superset ──
      'bc_l4_synthesis_failed','bc_l4_budget_exceeded','bc_cowork_stale',
      'bc_feature_drift','bc_calibration_drift','bc_synthesis_quality_decay'
    ]::text[]));
END $$;
