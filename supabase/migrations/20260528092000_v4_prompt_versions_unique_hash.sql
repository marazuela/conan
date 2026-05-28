-- v4 Phase 9b — UNIQUE constraint on (stage, prompt_hash) for race-safe
-- idempotent registration.
--
-- prompt_versions has lived on production since D-104/D-123 with 0 rows.
-- Phase 9b starts writing it from orchestrator_runtime.prompt_registry on
-- every Modal cold start. Multiple cold starts can fire concurrently
-- (Modal autoscales the orchestrator app); without a UNIQUE constraint
-- the registrar's "SELECT then INSERT if missing" pattern races and we
-- end up with duplicate rows for the same (stage, hash) tuple.
--
-- The constraint also lets the registrar use PostgREST's
-- `Prefer: resolution=merge-duplicates` UPSERT semantics so a single
-- round trip covers both the "already registered" and "first time"
-- paths.
--
-- Plan: ~/.claude/plans/phases-6-and-7-staged-hedgehog.md (Phase 9b).

alter table public.prompt_versions
  add constraint prompt_versions_stage_hash_unique
    unique (stage, prompt_hash);

comment on constraint prompt_versions_stage_hash_unique
  on public.prompt_versions is
  'Race-safe idempotency for orchestrator_runtime.prompt_registry: two '
  'concurrent registrars firing the same (stage, prompt_hash) UPSERT will '
  'converge to one row. Phase 9b dependency.';
