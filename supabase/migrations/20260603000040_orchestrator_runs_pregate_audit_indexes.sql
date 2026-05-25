-- =============================================================================
-- WI-2: BC convergence pre-gate — forensic indexes
--
-- The drainer is already protected from declined rows by the pre-existing
-- partial index orchestrator_runs_pending_idx (status='pending' on scheduled_at).
-- This migration adds two indexes that speed up the operator dashboard queries
-- the pre-gate's shadow-mode window relies on:
--
--   1) Decline-rate over time:
--        SELECT date_trunc('day', created_at), count(*)
--        FROM orchestrator_runs WHERE routine_declined = true ...
--
--   2) Score distribution for shadow-mode tuning:
--        SELECT date_trunc('day', created_at), avg(bc_pregate_score), ...
--        FROM orchestrator_runs WHERE bc_pregate_score IS NOT NULL ...
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md (WI-2)
-- =============================================================================

CREATE INDEX IF NOT EXISTS orchestrator_runs_declined_audit_idx
  ON public.orchestrator_runs (created_at DESC)
  WHERE routine_declined = true;

CREATE INDEX IF NOT EXISTS orchestrator_runs_bc_pregate_score_idx
  ON public.orchestrator_runs (created_at DESC, bc_pregate_score)
  WHERE bc_pregate_score IS NOT NULL;

COMMENT ON INDEX public.orchestrator_runs_declined_audit_idx IS
  'Speeds operator decline-rate dashboards. Partial — only indexes rows where evaluateBcPreGate() flipped routine_declined to true.';

COMMENT ON INDEX public.orchestrator_runs_bc_pregate_score_idx IS
  'Speeds shadow-mode tuning queries (avg/percentile of bc_pregate_score over 7-day window). Partial — only indexes rows the pre-gate actually scored.';
