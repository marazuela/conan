-- rpc_edgar_fetch — Supabase RPC wrapping the Modal edgar-fetch endpoint.
--
-- Context: signal_resolver must cite primary sources (SEC EDGAR). The
-- Cowork Claude-Code session's WebFetch tool is 403'd by sec.gov under SEC's
-- fair-access policy because WebFetch's default User-Agent doesn't contain a
-- contact email. This RPC provides a compliant-UA fetch path via the Modal
-- edgar_fetch_endpoint (modal_workers/app.py), which already has access to
-- the SEC_USER_AGENT secret via scanner-secrets — the same pattern every
-- in-worker EDGAR scanner uses.
--
-- Two-call pattern (enqueue → collect) matches the compute_rpcs convention
-- established in 20260427010000_compute_rpcs.sql; see the
-- _conan_modal_post_enqueue + rpc_compute_collect helpers for the mechanics.

insert into public.internal_config(key, value)
values ('modal_url_edgar_fetch', 'https://marazuela--edgar-fetch.modal.run')
on conflict (key) do update set value = excluded.value;

create or replace function public.rpc_edgar_fetch(url text)
returns bigint
language sql
security definer
set search_path to 'public', 'extensions', 'pg_temp'
as $function$
  select public._conan_modal_post_enqueue(
    'edgar_fetch',
    jsonb_build_object('url', url)
  );
$function$;

comment on function public.rpc_edgar_fetch(text) is
  'Enqueue a compliant-UA SEC/EDGAR fetch via the Modal edgar-fetch endpoint. '
  'Pair with rpc_compute_collect(request_id) to retrieve the response. '
  'Only sec.gov hosts are accepted by the endpoint; other URLs raise.';
