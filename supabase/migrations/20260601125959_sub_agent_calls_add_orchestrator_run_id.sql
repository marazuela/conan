-- sub_agent_calls: add orchestrator_run_id join column to enable assessment_id back-fill.
--
-- Context: 2026-05-27 found that all 18 sub_agent_calls rows had assessment_id=NULL even
-- when schema_pass=true. Stage 1 dispatches sub-agents BEFORE the parent
-- convergence_assessment is created in Stage 10, so no assessment_id exists at log_call
-- time. Without a join column, post-hoc back-fill is impossible (concurrent runs make
-- timestamp-range UPDATE unsafe). orchestrator_run_id is the natural key — every Stage 1
-- already knows its run.orchestrator_run_id, and Stage 10 returns the new assessment_id.
--
-- After this lands, sub_agent_dispatcher._log_call passes orchestrator_run_id at INSERT,
-- and runtime._run_one_inner calls backfill_assessment_id(orchestrator_run_id, assessment_id)
-- right after stage_10_persist returns. See operator_flag 4fc126c0.

alter table public.sub_agent_calls
  add column if not exists orchestrator_run_id uuid null;

create index if not exists sub_agent_calls_orchestrator_run_id_idx
  on public.sub_agent_calls (orchestrator_run_id)
  where orchestrator_run_id is not null;

comment on column public.sub_agent_calls.orchestrator_run_id is
  'FK-like reference to orchestrator_runs.id. Populated at INSERT time by sub_agent_dispatcher._log_call. Used to back-fill assessment_id after stage_10_persist creates the parent convergence_assessment.';
