-- =============================================================================
-- WI-2: BC convergence pre-gate — columns on orchestrator_runs + 'declined' status
--
-- Adds forensic columns so the reactor's evaluateBcPreGate() pre-dispatch
-- check can persist (a) why a given asset_document was declined and
-- (b) which inputs were null/zero at gate time, without forcing operators
-- to join through fda_pdufa_pipeline enrichment tables.
--
-- The pre-gate scores:
--   Breakthrough designation        +6
--   First-time sponsor (no P3 NDA)  +4
--   Class precedent (v1: stubbed 0; refresher table is follow-up PR)
-- Threshold v1: ≥6 (max 10). When the refresher table lands the max climbs
-- to 15 and threshold lifts to 9 — see plan WI-2 follow-up.
--
-- New status value 'declined' lives alongside the existing 'skipped_*' (didn't
-- run) and 'failed_*' (ran-but-errored) families. Naming chosen for semantic
-- parity with the existing routine_declined concept in thesis_jobs; the
-- drainer treats it identically to 'skipped_*' (filters on status='pending').
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md (WI-2)
-- =============================================================================

ALTER TABLE public.orchestrator_runs
  ADD COLUMN IF NOT EXISTS routine_declined boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS decline_reasons text[],
  ADD COLUMN IF NOT EXISTS bc_pregate_score numeric,
  ADD COLUMN IF NOT EXISTS bc_pregate_inputs jsonb;

-- Extend the status enum to include 'declined' (pre-gate rejection). The
-- existing constraint is dropped + recreated since CHECK constraints don't
-- support incremental ALTER.
ALTER TABLE public.orchestrator_runs
  DROP CONSTRAINT IF EXISTS orchestrator_runs_status_check;

ALTER TABLE public.orchestrator_runs
  ADD CONSTRAINT orchestrator_runs_status_check
  CHECK (status = ANY (ARRAY[
    'pending'::text,
    'running'::text,
    'completed'::text,
    'skipped_dedupe'::text,
    'skipped_budget'::text,
    'killed_budget'::text,
    'failed'::text,
    'failed_constitutional'::text,
    'declined'::text
  ]));

COMMENT ON COLUMN public.orchestrator_runs.status IS
  'pending | running | completed | skipped_dedupe | skipped_budget | killed_budget | failed | failed_constitutional | declined. The "declined" value is set by the reactor edge function evaluateBcPreGate() when an incoming asset_document fails the binary_catalyst convergence pre-gate (composite score below threshold, or enrichment_pending stub asset). No convergence_assessments row is created; the row exists for forensic audit only.';

COMMENT ON COLUMN public.orchestrator_runs.routine_declined IS
  'True when evaluateBcPreGate() rejected the run before dispatch. Mirrors the routine_declined concept on thesis_jobs / candidates. Pre-gate decline reasons live in decline_reasons; component scores in bc_pregate_inputs.';

COMMENT ON COLUMN public.orchestrator_runs.bc_pregate_score IS
  'Composite score from evaluateBcPreGate() (v1 max = 10: Breakthrough +6, first-time sponsor +4, class precedent stubbed +0). Threshold ≥6 to pass. Persisted on every binary_catalyst run, declined or not.';

COMMENT ON COLUMN public.orchestrator_runs.bc_pregate_inputs IS
  'JSONB recording the inputs the pre-gate observed at decision time: {breakthrough_designation: bool, first_time_sponsor: bool, class_precedent: 0|number|null, enrichment_state: "ready"|"stub"|"unavailable"}. Lets operators distinguish "scored low" from "data missing" without joining enrichment tables.';

-- Shadow-mode toggle. Default 'false' = pre-gate computes + persists score
-- but does NOT decline; flip to 'true' after 7 days when shadow-window FP rate
-- < 15%. See plan WI-2 verification section.
INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('bc_pregate_enabled', 'false', now())
ON CONFLICT (key) DO NOTHING;

-- Threshold the gate uses when active. 6 in v1 (max composite = 10 with
-- class_precedent stubbed to 0). When the bc_class_precedent_refresher table
-- lands (follow-up PR), max climbs to 15 and threshold should be bumped to 9.
INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('bc_pregate_threshold', '6', now())
ON CONFLICT (key) DO NOTHING;
