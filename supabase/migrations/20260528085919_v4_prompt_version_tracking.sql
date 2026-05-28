-- v4 Phase 9a — per-assessment prompt version tracking.
--
-- Adds two FK columns on convergence_assessments pointing at
-- public.prompt_versions so every assessment can be traced back to the
-- exact Stage 1 + Stage 9 prompt text it ran under. Enables Phase 9c's
-- quarterly retrospective to group resolved post_mortems by prompt
-- version and detect which prompt revisions correlate with which
-- accuracy regimes.
--
-- Why two columns: Stage 1 (synthesis) and Stage 9 (structured
-- extraction) evolve on different cadences. A Stage 9 schema tightening
-- shouldn't be conflated with a Stage 1 reasoning prompt change in the
-- post-mortem cohort. Each carries its own FK.
--
-- Both columns are nullable: (a) historical rows pre-date this tracking
-- and stay NULL; (b) the registrar in orchestrator_runtime.prompt_registry
-- is best-effort — if it can't UPSERT (e.g. transient Supabase error
-- during cold start), the assessment still persists with NULL FKs rather
-- than blocking the orchestrator. The Phase 9c retro filters NULL FKs
-- out of its cohort.
--
-- Plan: ~/.claude/plans/phases-6-and-7-staged-hedgehog.md (Phase 9a).

alter table public.convergence_assessments
  add column if not exists stage_1_prompt_version_id uuid
    references public.prompt_versions(id) on delete set null,
  add column if not exists stage_9_prompt_version_id uuid
    references public.prompt_versions(id) on delete set null;

comment on column public.convergence_assessments.stage_1_prompt_version_id is
  'FK to prompt_versions(id) for the Stage 1 (synthesis) prompt text this '
  'assessment ran under. Populated by orchestrator_runtime.prompt_registry '
  'at Stage 10 persist; NULL on historical rows pre-Phase-9a.';

comment on column public.convergence_assessments.stage_9_prompt_version_id is
  'FK to prompt_versions(id) for the Stage 9 (structured extraction) prompt '
  'text this assessment ran under. Populated by orchestrator_runtime.'
  'prompt_registry at Stage 10 persist; NULL on historical rows pre-Phase-9a.';

-- Indexes support Phase 9c retro queries that group resolved post_mortems
-- by prompt version over a 90-day cohort window. Partial — exclude NULL
-- rows from the index to keep it small (most historical rows are NULL).

create index if not exists idx_convergence_assessments_stage_1_prompt_version
  on public.convergence_assessments (stage_1_prompt_version_id, created_at desc)
  where stage_1_prompt_version_id is not null;

create index if not exists idx_convergence_assessments_stage_9_prompt_version
  on public.convergence_assessments (stage_9_prompt_version_id, created_at desc)
  where stage_9_prompt_version_id is not null;
