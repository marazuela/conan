-- Thesis challenger pass: a second Claude routine (different system prompt, "skeptical IC
-- reviewer" frame) that adjudicates drafts for semantic strength before promotion. Placed
-- PARALLEL to the syntactic gate (assess_thesis_v2) — both must pass. Closes the
-- ITRK-archetype failure mode (correct facts, no named asymmetry) that the syntactic gate
-- cannot catch.
--
-- Contract: draft → challenger → verdict ∈ {confirm, challenge, kill}
--   confirm   → proceed to syntactic gate (assess_thesis_v2).
--   challenge → 1 retry budget; drafter revises addressing required_fixes; re-challenge.
--   kill      → DLQ immediately, no retry (structural failure).
--
-- Retry budgets are NOW TWO INDEPENDENT counters:
--   attempt_count     — drafts submitted (existing; gate-retry budget)
--   challenge_count   — challenges performed on this job (new; challenge-retry budget)
--
-- Worst case per DLQ'd job = 2 drafts × 2 challenges = 4 Claude calls. Best case (confirm
-- on first try, gate passes first try) = 2 calls (draft + challenge). The 15/day cap is
-- per-promotion, not per-call, so this doesn't tighten throughput — only raises per-job
-- compute. Acceptable at 2-15/day steady-state volume.
--
-- Applies to three skills that produce or age theses:
--   - thesis_writer      — new Immediate-band drafts (primary path).
--   - signal_resolver    — inline drafts after rescore-to-immediate (shares thesis_writer's
--                          retry counters; same thesis_jobs row).
--   - candidate_aging    — challenger adjudicates Stage B `new_status='triggered'` claims
--                          BEFORE the existing regex integrity check, asking whether the
--                          matched signal is actually load-bearing for the kill condition
--                          or merely a cosmetic pattern hit. Uses candidate_aging_failures
--                          for its DLQ surface (error_kind='challenger_kill_cosmetic' or
--                          'challenger_kill_ambiguous'); no thesis_jobs column needed.
--
-- Data preserved in thesis_drafting_failures.all_drafts (existing jsonb): each array
-- element is now a {draft, gate_verdict, challenge_verdict} triple rather than a bare
-- draft object. No schema change on that column — convention-only.
--
-- History: this file was originally named 20260423000000_thesis_challenger.sql, which
-- collided with 20260423000000_realtime_publication.sql. Renamed to 20260423010000 to
-- make migration ordering deterministic. If the prior timestamp was already applied in
-- your DB, reconcile by inserting a row into supabase_migrations.schema_migrations with
-- version='20260423010000' so Supabase skips re-running. (The ALTER TABLE below is now
-- IF NOT EXISTS so a re-run is also safe.)

ALTER TABLE thesis_jobs
  ADD COLUMN IF NOT EXISTS challenge_count int NOT NULL DEFAULT 0;

COMMENT ON COLUMN thesis_jobs.challenge_count IS
  'Number of challenger passes performed on this job. Independent of attempt_count (which counts drafts submitted to the syntactic gate). Max 2 per job: a confirm/kill verdict on the first pass terminates without a second challenge; a challenge verdict grants one retry with required_fixes context.';
