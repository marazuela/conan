-- Phase 4B compute RPCs — Cowork ↔ Modal bridge.
--
-- Mirrors the v2 split-call pattern from
-- `20260429020000_compute_rpcs_split_call.sql`: each `rpc_<name>(args)`
-- enqueues an HTTP POST via pg_net + returns the request_id; the caller
-- pairs it with `rpc_compute_collect(request_id, max_wait_ms)` (already
-- exists from the v2 migration) to read back the response.
--
-- The endpoint name in `_conan_modal_post_enqueue('compute_v3', body)`
-- maps to `internal_config.key='modal_url_compute_v3'`. Unlike v2's
-- per-action endpoints, ALL Phase 4B compute actions go through the same
-- Modal endpoint (`compute-v3` on conan-v3-orchestrator) and the action
-- field inside the body selects the dispatch target. This keeps v3's
-- Modal `fastapi_endpoint` slot count at one (the workspace's 8-slot
-- free-tier cap is fully used by conan-v2; the multiplex pattern is
-- the only way to add v3 RPCs without freeing v2 slots or upgrading).
--
-- Required `internal_config` row before these RPCs work end-to-end:
--   key=modal_url_compute_v3, value=<deployed compute-v3 URL>
-- Operator seeds this AFTER `modal deploy modal_workers/orchestrator_app.py`
-- prints the compute-v3 endpoint URL.
--
-- See:
--   modal_workers/orchestrator_app.py::compute_v3_dispatch — the multiplex.
--   orchestrator_runtime/tier2.py — the runtime helpers each action calls.
--   orchestrator_runtime/ic_memo_runner.py — IC memo synthesis runner.
--   DECISIONS.md D-128 — Phase 4B foundation contract.

-- --------------------------------------------------------------------
-- 1. rpc_tier2_bulk_enqueue — Cowork's first call per cadence sweep.
--
-- Args:
--   asset_ids: text[] of fda_assets.id values to enqueue Tier-2 runs for.
-- Returns: bigint request_id (pair with rpc_compute_collect).
-- Modal action: tier2_bulk_enqueue.
-- --------------------------------------------------------------------

create or replace function public.rpc_tier2_bulk_enqueue(
  asset_ids text[]
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
    'compute_v3',
    jsonb_build_object(
      'action', 'tier2_bulk_enqueue',
      'args',   jsonb_build_object('asset_ids', to_jsonb(asset_ids))
    )
  );
$fn$;

comment on function public.rpc_tier2_bulk_enqueue(text[]) is
  'Phase 4B Cowork bridge — POSTs {action:tier2_bulk_enqueue,args:{asset_ids}} '
  'to compute_v3_dispatch. Returns pg_net request_id; pair with '
  'rpc_compute_collect(request_id) to read back the response.';

-- --------------------------------------------------------------------
-- 2. rpc_tier2_complete — Cowork posts a completed Tier-2 skill run.
--
-- Args:
--   run_id: orchestrator_runs.id (must be tier=2 — server enforces).
--   payload: convergence_assessment_v1.json from the bulk_orchestrator skill.
--   cost_usd: total Sonnet cost for the run (default 0).
--   latency_ms: wall-clock latency from skill start → end (nullable).
-- Returns: bigint request_id.
-- Modal action: tier2_complete.
-- --------------------------------------------------------------------

create or replace function public.rpc_tier2_complete(
  run_id      uuid,
  payload     jsonb,
  cost_usd    numeric default 0.0,
  latency_ms  int     default null
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
    'compute_v3',
    jsonb_build_object(
      'action', 'tier2_complete',
      'args',   jsonb_build_object(
        'run_id',      run_id::text,
        'payload',     payload,
        'cost_usd',    cost_usd,
        'latency_ms',  latency_ms
      )
    )
  );
$fn$;

comment on function public.rpc_tier2_complete(uuid, jsonb, numeric, int) is
  'Phase 4B Cowork bridge — POSTs {action:tier2_complete,...} to '
  'compute_v3_dispatch. Server validates payload, persists tier=2 row, '
  'applies §Escalation rule, marks run completed.';

-- --------------------------------------------------------------------
-- 3. rpc_tier2_fail — Cowork reports a Tier-2 skill error.
--
-- Args:
--   run_id: orchestrator_runs.id.
--   error_message: short description of the failure.
-- Returns: bigint request_id.
-- Modal action: tier2_fail.
-- --------------------------------------------------------------------

create or replace function public.rpc_tier2_fail(
  run_id         uuid,
  error_message  text default ''
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
    'compute_v3',
    jsonb_build_object(
      'action', 'tier2_fail',
      'args',   jsonb_build_object(
        'run_id',         run_id::text,
        'error_message',  error_message
      )
    )
  );
$fn$;

comment on function public.rpc_tier2_fail(uuid, text) is
  'Phase 4B Cowork bridge — POSTs {action:tier2_fail,...} to '
  'compute_v3_dispatch. Marks orchestrator_runs row failed (tier=2 only).';

-- --------------------------------------------------------------------
-- 4. rpc_ic_memo_run — Stage-11 synthesis on demand.
--
-- Args:
--   assessment_id: convergence_assessments.id to synthesize a memo for.
--   question: optional override for the synthesis prompt (default uses
--     the runner's DEFAULT_IC_MEMO_QUESTION).
--   persist: when false, skip the sub_agent_calls insert (dry-run).
-- Returns: bigint request_id.
-- Modal action: ic_memo_run.
-- --------------------------------------------------------------------

create or replace function public.rpc_ic_memo_run(
  assessment_id  uuid,
  question       text    default null,
  persist        boolean default true
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
    'compute_v3',
    jsonb_build_object(
      'action', 'ic_memo_run',
      'args',   jsonb_strip_nulls(jsonb_build_object(
        'assessment_id',  assessment_id::text,
        'question',       question,
        'persist',        persist
      ))
    )
  );
$fn$;

comment on function public.rpc_ic_memo_run(uuid, text, boolean) is
  'Phase 4B Cowork bridge — POSTs {action:ic_memo_run,...} to '
  'compute_v3_dispatch. Synthesizes an IC memo from the four specialist '
  'sub_agent_calls + Stage 9 thesis; persists to a fifth sub_agent_calls '
  'row with role=ic_memo (when persist=true).';

-- --------------------------------------------------------------------
-- 5. Permissions — service_role + authenticated only.
-- Mirrors the v2 RPC migration's grant/revoke shape.
-- --------------------------------------------------------------------

revoke execute on function public.rpc_tier2_bulk_enqueue(text[])           from public;
revoke execute on function public.rpc_tier2_complete(uuid, jsonb, numeric, int) from public;
revoke execute on function public.rpc_tier2_fail(uuid, text)               from public;
revoke execute on function public.rpc_ic_memo_run(uuid, text, boolean)     from public;

grant  execute on function public.rpc_tier2_bulk_enqueue(text[])           to service_role, authenticated;
grant  execute on function public.rpc_tier2_complete(uuid, jsonb, numeric, int) to service_role, authenticated;
grant  execute on function public.rpc_tier2_fail(uuid, text)               to service_role, authenticated;
grant  execute on function public.rpc_ic_memo_run(uuid, text, boolean)     to service_role, authenticated;

-- --------------------------------------------------------------------
-- 6. internal_config seed placeholder.
--
-- Idempotent INSERT ... ON CONFLICT DO NOTHING so re-running the migration
-- doesn't clobber a real URL the operator has already seeded. Operator
-- updates this row with the actual deployed URL after running:
--   modal deploy modal_workers/orchestrator_app.py
--
-- The placeholder value is unreachable, so any RPC call before the operator
-- updates it will fail with a clear pg_net transport error rather than
-- routing somewhere unintended.
-- --------------------------------------------------------------------

-- internal_config has only (key, value, updated_at) columns. The "what is
-- this row" comment lives in this migration file (above), not on the row.
insert into public.internal_config (key, value)
values (
  'modal_url_compute_v3',
  'https://placeholder--update-after-modal-deploy.invalid'
)
on conflict (key) do nothing;
