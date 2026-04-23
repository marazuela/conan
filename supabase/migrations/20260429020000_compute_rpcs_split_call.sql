-- Split-call replacement for the compute RPCs introduced in
-- 20260427010000_compute_rpcs.sql.
--
-- Why this migration exists:
--   The original `_conan_modal_post(text, jsonb)` called `net.http_post(...)`
--   and then `net.http_collect_response(req_id, async := false)` inside a
--   single plpgsql function body — one transaction. pg_net's background
--   worker reads `net.http_request_queue` from a separate connection under
--   READ COMMITTED, so it only sees committed rows. The enqueue INSERT was
--   invisible to the worker until the function returned, so the worker never
--   fired the HTTP request, `net._http_response` stayed empty, and
--   `http_collect_response` blocked forever — MCP killed every call at 60s.
--   Reproduced live on 2026-04-23; direct same-txn reproduction deadlocked,
--   split across two separate `execute_sql` calls it round-tripped in ~3s.
--
-- Fix: split each RPC into two Postgres calls.
--   1. `rpc_<name>(...) returns bigint` — enqueues via pg_net, returns the
--      request_id. The skill issues this as one `execute_sql` statement,
--      which auto-commits so the pg_net worker can see the queued row.
--   2. `rpc_compute_collect(request_id, max_wait_ms) returns jsonb` — polls
--      `net._http_response` directly (not via `http_collect_response`,
--      which is the primitive that deadlocks). Runs as a second, separate
--      `execute_sql` statement with its own snapshot; in READ COMMITTED each
--      poll iteration re-reads the table and sees the worker's committed
--      response row as soon as it lands.
--
-- Callers (must be edited in lockstep — all hardlinked at
--   Conan/.claude/skills/<name>.md ↔ conan-cowork-skills/skills/<name>.md):
--   signal_resolver  (step 6 rescore; step 11 chain if inlined)
--   thesis_writer    (step 7 assess; step 8a render → upload)
--   candidate_aging  (step 6 regex_check)
--   coverage_auditor (step 6 storage_upload)
--
-- Non-goals / what this does NOT change:
--   - Modal endpoints (`modal_workers/app.py`) are untouched — the bug was
--     purely on the pg_net side.
--   - `internal_config` rows (`modal_url_*`, `compute_secret`) are reused
--     as-is; no reseed required.
--   - Fan-out reactor → Modal calls via Deno `fetch` (rubric_apply_caps,
--     health) never went through pg_net and are unaffected.
--
-- pg_net version pinning:
--   Tested against pg_net 0.20.0. The response table name `net._http_response`
--   has moved in past pg_net releases. If Supabase bumps pg_net, re-verify
--   this table name (and the `(id, status_code, timed_out, error_msg,
--   content)` column set) before assuming this migration still works.

-- --------------------------------------------------------------------
-- 1. Drop the broken single-call helpers + typed wrappers.
--
-- Postgres overloads functions on argument list, not on return type, so
-- recreating the same names with `returns bigint` requires dropping the
-- old `returns jsonb` signatures first.
-- --------------------------------------------------------------------

drop function if exists public.rpc_rescore_with_dims(text, jsonb, jsonb, text);
drop function if exists public.rpc_assess_thesis(jsonb);
drop function if exists public.rpc_render_candidate_markdown(jsonb);
drop function if exists public.rpc_regex_check(text, text);
drop function if exists public.rpc_storage_upload(text, text, text, text);
drop function if exists public._conan_modal_post(text, jsonb);

-- --------------------------------------------------------------------
-- 2. Enqueue helper: config lookup + pg_net fire-and-return.
--
-- Returns the bigint request_id so the caller (or wrapper) can hand it to
-- `rpc_compute_collect`. `timeout_milliseconds` is set to 30000 to match the
-- Modal endpoints' server-side `timeout=30` — pg_net's default is 5000ms,
-- which trips on Modal cold-starts (observed 4.8s on rescore_with_dims first
-- hit, 2026-04-23). The collector's `max_wait_ms` (default 40000) bounds the
-- skill-facing poll. If pg_net's own transport limits surface (DNS, TLS,
-- timeout), it writes a row with `error_msg IS NOT NULL`, which the
-- collector treats as terminal.
-- --------------------------------------------------------------------

create or replace function public._conan_modal_post_enqueue(
  endpoint text,  -- e.g. 'rescore_with_dims' — looked up via internal_config.key='modal_url_<endpoint>'
  body jsonb
)
returns bigint
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $fn$
declare
  v_config_key text := 'modal_url_' || endpoint;
  v_url        text;
  v_secret     text;
  v_req_id     bigint;
begin
  select value into v_url
    from public.internal_config
   where key = v_config_key;

  if v_url is null then
    raise exception '_conan_modal_post_enqueue: internal_config.% is not set', v_config_key;
  end if;

  -- Fail-closed: every Modal compute endpoint enforces x-conan-compute-secret.
  -- Missing secret means "this environment isn't wired up" — raise rather than
  -- POST without the header and eat a 401 from Modal.
  select value into v_secret
    from public.internal_config
   where key = 'compute_secret';

  if v_secret is null or v_secret = '' then
    raise exception '_conan_modal_post_enqueue: internal_config.compute_secret is not set';
  end if;

  -- pg_net's default timeout_milliseconds is 5000 — too tight for Modal cold-starts
  -- (observed 4.8s on rescore_with_dims first-hit, 2026-04-23). 30000 matches the
  -- Modal endpoint's server-side timeout=30. The collector's max_wait_ms (40000)
  -- bounds the skill-facing poll.
  select net.http_post(
    url                  := v_url,
    body                 := body,
    headers              := jsonb_build_object(
                              'Content-Type',            'application/json',
                              'x-conan-compute-secret',  v_secret
                            ),
    timeout_milliseconds := 30000
  ) into v_req_id;

  return v_req_id;
end;
$fn$;

comment on function public._conan_modal_post_enqueue(text, jsonb) is
  'Enqueue half of the split-call Modal compute RPC. Looks up Modal URL via internal_config.key=''modal_url_<endpoint>'' and the shared secret via internal_config.key=''compute_secret'' (sent as x-conan-compute-secret). Returns the pg_net request_id. Pair with rpc_compute_collect(request_id).';

-- --------------------------------------------------------------------
-- 3. Collect helper: poll `net._http_response` and raise/return.
--
-- Single source of truth for the wait-for-reply half. Reads the table
-- directly rather than calling net.http_collect_response(), which is the
-- primitive that deadlocks when called in the same transaction as
-- net.http_post. Here we're in a separate execute_sql statement, so each
-- `select ... where id = request_id` iteration re-snapshots under READ
-- COMMITTED and sees the worker's committed row as soon as it lands.
-- --------------------------------------------------------------------

create or replace function public.rpc_compute_collect(
  request_id   bigint,
  max_wait_ms  int default 40000  -- ≥20s headroom under MCP's 60s execute_sql cap for network roundtrip + Modal cold-start slack
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $fn$
declare
  v_poll_interval_ms constant int := 250;  -- Modal warm-path p50 well under 2s; 250ms keeps common-case latency tight.
  v_max_iterations   int;
  v_i                int := 0;
  v_status_code      int;
  v_timed_out        boolean;
  v_error_msg        text;
  v_content          text;
begin
  if request_id is null then
    raise exception 'rpc_compute_collect: request_id must not be null';
  end if;

  v_max_iterations := greatest(1, max_wait_ms / v_poll_interval_ms);

  loop
    select status_code, timed_out, error_msg, content
      into v_status_code, v_timed_out, v_error_msg, v_content
      from net._http_response
     where id = request_id;

    if found then
      -- pg_net transport failure (DNS, TLS, timeout at the worker's own
      -- limit). Surface as terminal — skill-level retry via sweeper.
      if v_error_msg is not null then
        raise exception 'rpc_compute_collect: request_id=% pg_net transport error: %',
          request_id, v_error_msg;
      end if;

      if v_timed_out then
        raise exception 'rpc_compute_collect: request_id=% pg_net timed_out',
          request_id;
      end if;

      if v_status_code = 200 then
        return v_content::jsonb;
      end if;

      -- Any non-200 (4xx or 5xx): raise with the body truncated so the skill
      -- can classify (modal_4xx / modal_5xx / payload_invalid / etc.) via the
      -- Postgres error message. No internal 502/503/504 retry — Modal
      -- redeploy blips now flow through attempt_count + sweeper semantics,
      -- same path every other skill error already takes.
      raise exception 'rpc_compute_collect: request_id=% HTTP %: %',
        request_id, v_status_code, left(coalesce(v_content, ''), 500);
    end if;

    v_i := v_i + 1;
    if v_i >= v_max_iterations then
      raise warning 'rpc_compute_collect: request_id=% orphaned — no response row after %ms',
        request_id, max_wait_ms;
      raise exception 'rpc_compute_collect: request_id=% timed out after %ms with no net._http_response row',
        request_id, max_wait_ms;
    end if;

    perform pg_sleep(v_poll_interval_ms::numeric / 1000);
  end loop;
end;
$fn$;

comment on function public.rpc_compute_collect(bigint, int) is
  'Collect half of the split-call Modal compute RPC. Polls net._http_response (pg_net 0.20.0) at 250ms intervals up to max_wait_ms, returning the 200 body as jsonb. Raises on any non-200, pg_net transport error, or timeout. Defaults to 40000ms, leaving ≥20s headroom under MCP execute_sql 60s cap.';

-- --------------------------------------------------------------------
-- 4. Thin RPC wrappers — one per Modal endpoint. All return bigint.
--
-- Same argument shapes as the old `returns jsonb` versions, so every
-- existing $json$…$json$::jsonb dollar-quote block in the skills is
-- reusable verbatim — only the second statement (the collect) is new.
-- --------------------------------------------------------------------

-- signal_resolver step 6
create or replace function public.rpc_rescore_with_dims(
  scoring_profile text,
  raw_payload     jsonb,
  dims            jsonb,
  provenance      text default 'ai_resolved'
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
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
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
    'assess_thesis',
    jsonb_build_object('thesis', thesis)
  );
$fn$;

-- thesis_writer step 8a, signal_resolver step 11
-- `args` passthrough: {signal, thesis, band, scoring_profile, entity?}
create or replace function public.rpc_render_candidate_markdown(args jsonb)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue('render_candidate_markdown', args);
$fn$;

-- candidate_aging step 6
create or replace function public.rpc_regex_check(
  pattern        text,
  text_to_search text
)
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
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
returns bigint
language sql
security definer
set search_path = public, extensions, pg_temp
as $fn$
  select public._conan_modal_post_enqueue(
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
-- 5. Grants — service_role only. Supabase MCP talks as service_role.
-- --------------------------------------------------------------------

revoke execute on function public._conan_modal_post_enqueue(text, jsonb)                 from public;
revoke execute on function public.rpc_compute_collect(bigint, int)                       from public;
revoke execute on function public.rpc_rescore_with_dims(text, jsonb, jsonb, text)        from public;
revoke execute on function public.rpc_assess_thesis(jsonb)                               from public;
revoke execute on function public.rpc_render_candidate_markdown(jsonb)                   from public;
revoke execute on function public.rpc_regex_check(text, text)                            from public;
revoke execute on function public.rpc_storage_upload(text, text, text, text)             from public;

grant  execute on function public._conan_modal_post_enqueue(text, jsonb)                 to service_role;
grant  execute on function public.rpc_compute_collect(bigint, int)                       to service_role;
grant  execute on function public.rpc_rescore_with_dims(text, jsonb, jsonb, text)        to service_role;
grant  execute on function public.rpc_assess_thesis(jsonb)                               to service_role;
grant  execute on function public.rpc_render_candidate_markdown(jsonb)                   to service_role;
grant  execute on function public.rpc_regex_check(text, text)                            to service_role;
grant  execute on function public.rpc_storage_upload(text, text, text, text)             to service_role;
