-- Phase 4B follow-up — add 'ic_memo' to sub_agent_calls.role CHECK.
--
-- Phase 2 (20260506000020_v3_phase_2_orchestrator_schema.sql line 192-193)
-- restricted role to the four research specialists:
--   ('literature','competitive','regulatory_history','options_microstructure')
--
-- Phase 3A added a fifth runner: ICMemoRunner
-- (modal_workers/sub_agents/ic_memo.py, role='ic_memo'). It's a synthesis-only
-- sub-agent that consumes the four specialists' outputs + Stage 9 thesis and
-- emits a memo conforming to ic_memo_v1.json. It needs to persist alongside
-- the four specialists for the dashboard's <SubAgentPanels /> to render the
-- always-expanded IC memo panel (D-111 §2 layout).
--
-- This migration widens the CHECK to include 'ic_memo'. Existing rows
-- (which can only have one of the four specialist roles) are unaffected by
-- the wider constraint.
--
-- Rollback: ALTER TABLE sub_agent_calls DROP CONSTRAINT sub_agent_calls_role_check;
-- then re-add the original four-role CHECK. Safe because no live row carries
-- role='ic_memo' until the runtime entry point ships.

ALTER TABLE public.sub_agent_calls
  DROP CONSTRAINT IF EXISTS sub_agent_calls_role_check;

ALTER TABLE public.sub_agent_calls
  ADD CONSTRAINT sub_agent_calls_role_check
  CHECK (role IN (
    'literature',
    'competitive',
    'regulatory_history',
    'options_microstructure',
    'ic_memo'
  ));

COMMENT ON COLUMN public.sub_agent_calls.role IS
  'Sub-agent role: 4 research specialists (literature/competitive/'
  'regulatory_history/options_microstructure) + 1 synthesis sub-agent '
  '(ic_memo). The IC memo synthesizes the specialists'' outputs + the '
  'Stage 9 thesis into a memo matching ic_memo_v1.json. Phase 4B '
  '(2026-05-08).';
