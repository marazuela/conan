-- Compute RPCs for Cowork-scheduled skills
--
-- Context: the Cowork Linux sandbox stopped starting on 2026-04-22, breaking
-- every scheduled skill that shelled out to `python3 -c` or `curl`. This
-- migration routes those helpers through Postgres RPCs that POST to Modal
-- HTTP endpoints via pg_net. Skills keep their `mcp__supabase__execute_sql`
-- surface unchanged.
--
-- Flow:   skill -> execute_sql -> rpc_* -> _conan_modal_post -> pg_net -> Modal
-- Modal:  modal_workers/app.py::{rescore_with_dims_endpoint,
--         assess_thesis_endpoint, render_candidate_markdown_endpoint,
--         regex_check_endpoint, storage_upload_endpoint}
--
-- Prereqs (verified 2026-04-22 via mcp__supabase__list_extensions):
--   pg_net 0.20.0 installed in schema "extensions" — no CREATE EXTENSION needed.

-- --------------------------------------------------------------------
-- 1. Config table
--
-- Rows:
--   key = 'modal_url_<endpoint>'  — per-function Modal URL
--                                   (e.g. `https://marazuela--<label>.modal.run`).
--   key = 'compute_secret'        — shared secret injected as the
--                                   `x-conan-compute-secret` header on every
--                                   Modal compute POST. MUST match
--                                   `CONAN_COMPUTE_SECRET` in the Modal
--                                   `compute-auth` secret or every RPC 401s.
--                                   Seed this row BEFORE deploying this migration
--                                   to an environment where skills are active;
--                                   otherwise the functions raise on first call.
-- Service-role access only (RLS denies anon/authenticated; no policies granted).
-- --------------------------------------------------------------------

create table if not exists public.internal_config (
  key        text        primary key,
  value      text        not null,
  updated_at timestamptz not null default now()
);

alter table public.internal_config enable row level security;

-- Reuse existing trigger function from initial schema (set_updated_at()).
drop trigger if exists internal_config_set_updated_at on public.internal_config;
create trigger internal_config_set_updated_at
  before update on public.internal_config
  for each row execute function public.set_updated_at();

-- No RLS policies — default deny for anon/authenticated. service_role bypasses
-- RLS so skills (via Supabase MCP) can still read.

comment on table public.internal_config is
  'Runtime config for compute-RPC proxies. Keys: modal_url_<endpoint>, compute_secret. Service-role access only; RLS denies anon/authenticated.';

-- --------------------------------------------------------------------
-- 2. Shared HTTP helper: POST to Modal, sync-wait, retry once on 5xx.
--
-- Uses the PUBLIC net.http_collect_response (not the underscored private
-- `_http_collect_response`) to avoid coupling to Supabase's internal API.
-- --------------------------------------------------------------------

create or replace function public._conan_modal_post(
  endpoint text,  -- e.g. 'rescore_with_dims' — looked up as internal_config.key='modal_url_<endpoint>'
  body jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $fn$
declare
  v_config_key  text := 'modal_url_' || endpoint;
  v_url         text;
  v_secret      text;
  v_req_id      bigint;
  v_result      net.http_response_result;
  v_status_code int;
  v_body        text;
  v_attempt     int := 0;
begin
  select value into v_url
    from public.internal_config
   where key = v_config_key;

  if v_url is null then
    raise exception '_conan_modal_post: internal_config.% is not set', v_config_key;
  end if;

  -- Fail-closed: the Modal endpoints require x-conan-compute-secret. Missing
  -- secret means "this environment isn't configured yet" — raising here is
  -- preferable to POSTing without the header and getting a 401 from Modal.
  select value into v_secret
    from public.internal_config
   where key = 'compute_secret';

  if v_secret is null or v_secret = '' then
    raise exception '_conan_modal_post: internal_config.compute_secret is not set (seed before applying this migration)';
  end if;

  loop
    v_attempt := v_attempt + 1;

    -- Fire the request. net.http_post returns a bigint request_id. Overriding
    -- `headers` replaces the default Content-Type, so re-declare it here.
    select net.http_post(
      url                  := v_url,
      body                 := body,
      headers              := jsonb_build_object(
                                'Content-Type',            'application/json',
                                'x-conan-compute-secret',  v_secret
                              ),
      timeout_milliseconds := 30000
    ) into v_req_id;

    -- Synchronously wait for the response (async := false blocks the worker).
    select * into v_result
      from net.http_collect_response(v_req_id, async := false);

    -- Transport-level failure (DNS, TLS, hard timeout): don't retry — surface.
    if v_result.status <> 'SUCCESS' then
      raise exception '_conan_modal_post: % request_id=% status=% message=%',
        endpoint, v_req_id, v_result.status, v_result.message;
    end if;

    v_status_code := (v_result.response).status_code;
    v_body        := (v_result.response).body;

    if v_status_code = 200 then
      return v_body::jsonb;
    elsif v_status_code in (502, 503, 504) and v_attempt < 2 then
      -- Modal redeploy window or platform blip — one retry with backoff.
      perform pg_sleep(2);
      continue;
    else
      raise exception '_conan_modal_post: % returned HTTP %: %',
        endpoint, v_status_code, v_body;
    end if;
  end loop;
end;
$fn$;

comment on function public._conan_modal_post(text, jsonb) is
  'Shared POST helper for rpc_* skill proxies. Looks up Modal URL via internal_config.key=''modal_url_<endpoint>'' and the shared secret via internal_config.key=''compute_secret'' (sent as x-conan-compute-secret). Retries once on 502/503/504. Raises on other non-200 or any missing config.';

-- --------------------------------------------------------------------
-- 3. Thin RPC wrappers — one per Modal endpoint. All return jsonb.
-- --------------------------------------------------------------------

-- signal_resolver step 6
create or replace function public.rpc_rescore_with_dims(
  scoring_profile text,
  raw_payload     jsonb,
  dims            jsonb,
  provenance      text default 'ai_resolved'
)
returns jsonb
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post(
    'rescore_with_dims',
    jsonb_build_object(
      'scoring_profile', scoring_profile,
      'raw_payload',     raw_payload,
      'dims',            dims,
      'provenance',      provenance
    )
  );
$fn$;

-- thesis_writer step 7, signal_resolver step 11
create or replace function public.rpc_assess_thesis(thesis jsonb)
returns jsonb
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post(
    'assess_thesis',
    jsonb_build_object('thesis', thesis)
  );
$fn$;

-- thesis_writer step 8a, signal_resolver step 11
-- `args` passthrough: {signal, thesis, band, scoring_profile, entity?}
create or replace function public.rpc_render_candidate_markdown(args jsonb)
returns jsonb
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post('render_candidate_markdown', args);
$fn$;

-- candidate_aging step 6
create or replace function public.rpc_regex_check(
  pattern        text,
  text_to_search text
)
returns jsonb
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post(
    'regex_check',
    jsonb_build_object('pattern', pattern, 'text', text_to_search)
  );
$fn$;

-- coverage_auditor step 6 + thesis_writer step 8a dossier upload
create or replace function public.rpc_storage_upload(
  bucket       text,
  path         text,
  content      text,
  content_type text default 'text/markdown'
)
returns jsonb
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post(
    'storage_upload',
    jsonb_build_object(
      'bucket',       bucket,
      'path',         path,
      'content',      content,
      'content_type', content_type
    )
  );
$fn$;

-- --------------------------------------------------------------------
-- 4. Grants — service_role only. Supabase MCP talks as service_role.
-- --------------------------------------------------------------------

revoke execute on function public._conan_modal_post(text, jsonb)                        from public;
revoke execute on function public.rpc_rescore_with_dims(text, jsonb, jsonb, text)       from public;
revoke execute on function public.rpc_assess_thesis(jsonb)                              from public;
revoke execute on function public.rpc_render_candidate_markdown(jsonb)                  from public;
revoke execute on function public.rpc_regex_check(text, text)                           from public;
revoke execute on function public.rpc_storage_upload(text, text, text, text)            from public;

grant  execute on function public._conan_modal_post(text, jsonb)                        to service_role;
grant  execute on function public.rpc_rescore_with_dims(text, jsonb, jsonb, text)       to service_role;
grant  execute on function public.rpc_assess_thesis(jsonb)                              to service_role;
grant  execute on function public.rpc_render_candidate_markdown(jsonb)                  to service_role;
grant  execute on function public.rpc_regex_check(text, text)                           to service_role;
grant  execute on function public.rpc_storage_upload(text, text, text, text)            to service_role;
