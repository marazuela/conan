-- Seed internal_config.sub_agent_role_controls — the DB-backed per-role sub-agent
-- kill switch read by orchestrator_runtime.sub_agent_dispatcher._read_role_controls().
--
-- Value is a JSON object {role: enabled_bool}; an ABSENT role => enabled.
-- VALUES MUST BE JSON BOOLEANS (true/false) — a string ("false") coerces to True.
-- The DB control is AUTHORITATIVE when it names a role (can disable OR re-enable a
-- role at runtime with no Modal redeploy); the ORCH_DISABLE_<ROLE> env var is the
-- fallback only for roles the DB omits.
--
-- Toggle a role live (takes effect on the next orchestrator run, ~<=60s + drain cadence):
--   update public.internal_config
--      set value = '{"literature": false, "commercial_opportunity": true}', updated_at = now()
--    where key = 'sub_agent_role_controls';
--
-- Initial seed disables the two roles with near-zero schema_pass yield
-- (literature 0/13, commercial_opportunity 2/18) that drove the 2026-06-01/02 burn.
-- Keep the productive roles (competitive, regulatory_history, options_microstructure)
-- enabled by simply not listing them.
--
-- NOTE: inert until the reader code (this branch) is deployed to the orchestrator
-- Modal app. Applied live as an MCP one-shot (data seed, not code DDL) per
-- docs/MIGRATIONS.md §6; this file is the tracked record.
insert into public.internal_config (key, value, updated_at)
values (
  'sub_agent_role_controls',
  '{"literature": false, "commercial_opportunity": false}',
  now()
)
on conflict (key) do nothing;  -- PK is (key): seed-once, never clobber a live operator edit
