-- 20260526010000_orchestrator_runs_config_overrides.sql
-- Wave 9.1 — per-run knob overrides for the v3 orchestrator.
--
-- Today, runtime constants (SUB_AGENT_LOOP_MAX_TURNS, ENABLE_SUB_AGENTS,
-- ENABLE_STAGE_1_RAG, etc.) are module globals. To tune per asset or per
-- one-off operator-refresh without a redeploy, we need a per-run lever.
--
-- This adds a jsonb column to orchestrator_runs that runtime.py reads at
-- the start of an assessment and stashes on ctx for downstream stages to
-- consult. The dashboard's "advanced" run-trigger form will eventually
-- write into this column; until then it's NULL for cron-enqueued rows
-- and the runtime falls back to the module defaults.
--
-- The column name mirrors orchestrator_runs.notes (jsonb, NULL-default) so
-- the on-disk shape stays consistent across operator/system-authored fields.
--
-- Recognized keys (this list is documentation only — the runtime ignores
-- unknown keys without error):
--   sub_agent_max_turns      int        — overrides SUB_AGENT_LOOP_MAX_TURNS
--   enable_sub_agents        bool       — overrides ENABLE_SUB_AGENTS_DEFAULT
--   enable_stage_1_rag       bool       — overrides ENABLE_STAGE_1_RAG_DEFAULT
--   ensemble_n               int        — overrides the trigger-mapped N
--   ensemble_mode            text       — overrides 'streaming' | 'batch'
--   all_falsified_ceiling    numeric    — overrides ALL_FALSIFIED_CONVICTION_CEILING

ALTER TABLE public.orchestrator_runs
  ADD COLUMN IF NOT EXISTS config_overrides jsonb;

COMMENT ON COLUMN public.orchestrator_runs.config_overrides IS
  'Per-run knob overrides read by orchestrator_runtime.runtime at the start '
  'of an assessment. See migration 20260526010000 for the recognized key list. '
  'Unknown keys are ignored without error.';
