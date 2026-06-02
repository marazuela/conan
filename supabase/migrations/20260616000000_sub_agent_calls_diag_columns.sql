-- Persist sub-agent diagnostics so failures are debuggable from the DB instead
-- of live Modal-log spelunking. (memory sub_agent_schema_drift_2026-05-23.md Round-6)
--   final_text  = the model's last-turn text that failed to parse; EMPTY => the
--                 model never emitted a synthesis turn (the commercial {} mode).
--   stop_reason = why the loop ended (end_turn / tool_use / max_turns / budget).
--   errors      = validation/runtime errors for this call (previously dropped,
--                 which made zero-token early crashes structurally invisible).
-- Additive + idempotent. Applied live via MCP one-shot (supabase db push is
-- drift/WIP-hazardous in this repo) and tracked here for reconciliation.
alter table public.sub_agent_calls
  add column if not exists final_text  text,
  add column if not exists stop_reason text,
  add column if not exists errors      jsonb;

comment on column public.sub_agent_calls.final_text is
  'Truncated last-turn text from the sub-agent loop; empty => model never emitted a synthesis turn.';
comment on column public.sub_agent_calls.stop_reason is
  'Anthropic stop_reason of the final turn (end_turn/tool_use/max_turns/budget) — why the loop ended.';
comment on column public.sub_agent_calls.errors is
  'Validation/runtime errors for this call (jsonb array). Surfaces zero-token early crashes that were previously invisible.';
