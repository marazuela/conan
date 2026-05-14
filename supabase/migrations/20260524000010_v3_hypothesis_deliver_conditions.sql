-- 20260524000010_v3_hypothesis_deliver_conditions.sql
-- Stage 2 symmetric counterpart to kill_conditions.
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M2)
--
-- v3 hypothesis_enumeration already carries kill_conditions but is missing the
-- symmetric deliver_conditions field. v2 candidates.deliver_conditions was
-- added in 20260517000000 to close the AXSM stuck-active loop (deliver-side
-- triggers were previously implicit). Mirror that on the v3 per-hypothesis
-- table so the orchestrator stage 2 prompt can emit both arrays and the
-- aging Stage B Cowork skill can evaluate them symmetrically.

ALTER TABLE public.hypothesis_enumeration
  ADD COLUMN IF NOT EXISTS deliver_conditions jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_name = 'hypothesis_enumeration_deliver_conditions_array_check'
  ) THEN
    ALTER TABLE public.hypothesis_enumeration
      ADD CONSTRAINT hypothesis_enumeration_deliver_conditions_array_check
      CHECK (jsonb_typeof(deliver_conditions) = 'array');
  END IF;
END $$;

COMMENT ON COLUMN public.hypothesis_enumeration.deliver_conditions IS
  'v3 Stage 2: symmetric counterpart to kill_conditions. Each element is an '
  'observable that, when triggered, confirms the upside thesis (FDA approval '
  'letter, deal-close 8-K, Phase 3 met primary endpoint, etc.). Mirrors '
  'v2 candidates.deliver_conditions shape: '
  '{id, description, observable: {source_type, search_pattern}, date_bound, status}.';
