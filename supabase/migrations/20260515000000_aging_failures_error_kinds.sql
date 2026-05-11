-- P0 #5: widen candidate_aging_failures.error_kind CHECK constraint to match
-- the values the candidate_aging.md skill actually writes
-- (challenger_kill_cosmetic, challenger_kill_ambiguous, challenger_challenge,
--  challenger_budget_exhausted, streak_reset).
--
-- This file documents intent for repo bookkeeping. The live DB constraint
-- already includes these values as of 2026-05-08; the DROP/ADD pair is
-- idempotent so re-applying is safe and a fresh local DB build will land
-- in the correct state.
--
-- Memory ref: supabase_migrations_drift.md — live DB is ahead of local files;
-- always verify current state via MCP before assuming a constraint is missing.

ALTER TABLE public.candidate_aging_failures
  DROP CONSTRAINT IF EXISTS candidate_aging_failures_error_kind_check;

ALTER TABLE public.candidate_aging_failures
  ADD CONSTRAINT candidate_aging_failures_error_kind_check
  CHECK (error_kind = ANY (ARRAY[
    'routine_error'::text,
    'routine_declined'::text,
    'hallucinated_trigger'::text,
    'quota_exhausted'::text,
    'gate_mismatch'::text,
    'streak_reset'::text,
    'challenger_kill_cosmetic'::text,
    'challenger_kill_ambiguous'::text,
    'challenger_challenge'::text,
    'challenger_budget_exhausted'::text,
    'other'::text
  ]));
