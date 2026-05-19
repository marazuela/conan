-- F1b — temporal directional convergence (v2 capability preservation).
--
-- Additive + reversible. Adds one nullable jsonb column to
-- convergence_assessments. Stage 10 (orchestrator_runtime/runtime.py)
-- writes a payload of shape:
--   { "verdict": "same_direction"|"contradiction"|"single",
--     "n_priors": int, "n_unique": int, "modifier_pp": float,
--     "contradiction": bool, "prior_ids": [text, ...] }
--
-- Existing rows read back NULL (no backfill — historical runs had no
-- temporal-convergence step). The conviction modifier is applied to
-- conviction_pct_calibrated BEFORE band assignment in Stage 10; this
-- column is the audit trail + the source for the dashboard / IC-memo
-- contradiction flag.
--
-- DEPLOY ORDERING (enforced manually, gated): apply THIS migration before
-- deploying the orchestrator_runtime change that emits the column key.
-- The runtime adds "directional_convergence" to the persist row dict
-- unconditionally; persisting before the column exists would fail the
-- PostgREST insert. Additive-then-deploy is the standard sequence.
--
-- ROLLBACK: `ALTER TABLE public.convergence_assessments
--            DROP COLUMN IF EXISTS directional_convergence;`
-- Safe at any time — no FK, no view/RPC depends on it (new column).
-- This is NOT part of the Phase-D v2 drop migration (separate concern,
-- separate file) and must survive Phase D.

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS directional_convergence jsonb;

COMMENT ON COLUMN public.convergence_assessments.directional_convergence IS
  'F1b temporal directional-convergence verdict (capability preservation '
  'of v2 reactor convergence). Pure-derived in Stage 10 from this asset''s '
  'prior non-superseded assessments via rubric_engine.convergence_reference. '
  'NULL for pre-F1b rows. contradiction=true penalizes + flags, never '
  'hard-caps.';
