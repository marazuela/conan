-- sub_agent_calls.role CHECK constraint: add commercial_opportunity.
--
-- Found 2026-06-01 during the post-PR-#174 VRDN dry-run prep: the live CHECK
-- constraint allowed (literature, competitive, regulatory_history,
-- options_microstructure, ic_memo) but NOT commercial_opportunity. The 5th
-- Stage-1 role's runner + skill prompt + schema all exist, but every dispatch
-- INSERT 23514'd at the constraint and silently fell through to
-- failed_reactor_events instead of sub_agent_calls. Hidden behind the
-- assessment_id=NULL gap which we just fixed (PR #174) — without that fix,
-- nobody noticed commercial_opportunity rows were missing because no rows
-- landed for any role.
--
-- Live DDL was applied via execute_sql on 2026-06-01; this file makes the
-- change disk-tracked so future supabase db push is idempotent.
--
-- Refs: operator_flag 4fc126c0, PR #174.

alter table public.sub_agent_calls drop constraint if exists sub_agent_calls_role_check;

alter table public.sub_agent_calls add constraint sub_agent_calls_role_check
  check (role = any (array[
    'literature'::text,
    'competitive'::text,
    'regulatory_history'::text,
    'options_microstructure'::text,
    'commercial_opportunity'::text,
    'ic_memo'::text
  ]));
