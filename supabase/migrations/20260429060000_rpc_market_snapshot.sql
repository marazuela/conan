-- rpc_market_snapshot — Supabase RPC for the "market_snapshot" dispatch kind
-- of the Modal compute-fetch endpoint (currently labelled edgar-fetch to avoid
-- breaking in-flight rpc_edgar_fetch calls during the consolidation).
--
-- Context: the litigation profile's `financial_materiality` dim is damages /
-- market_cap, but `entities.market_cap_usd` has no writer anywhere in the
-- codebase — 1,119 entities, 100% NULL as of 2026-04-23. Rather than add an
-- entity enricher backfill (which would churn infrequently-used rows every
-- night), the resolver fetches a live snapshot per-litigation-signal via this
-- RPC, using the same `load_market_snapshot` helper that in-worker heuristic
-- scoring already uses.
--
-- Dispatcher pattern: the edgar-fetch endpoint handles two `kind` values
-- ('edgar_fetch' default, 'market_snapshot'). Both internal_config URLs point
-- at the same endpoint — we pay a slightly more verbose payload to stay within
-- Modal's 8-endpoint cap without a plan upgrade.
--
-- Two-call pattern (enqueue → collect) matches the compute_rpcs convention
-- from 20260427010000_compute_rpcs.sql; see _conan_modal_post_enqueue +
-- rpc_compute_collect for the mechanics.

insert into public.internal_config(key, value)
values ('modal_url_market_snapshot', 'https://marazuela--edgar-fetch.modal.run')
on conflict (key) do update set value = excluded.value;

create or replace function public.rpc_market_snapshot(ticker text, mic text default null)
returns bigint
language sql
security definer
set search_path to 'public', 'extensions', 'pg_temp'
as $function$
  select public._conan_modal_post_enqueue(
    'market_snapshot',
    jsonb_build_object('kind', 'market_snapshot', 'ticker', ticker, 'mic', mic)
  );
$function$;

comment on function public.rpc_market_snapshot(text, text) is
  'Enqueue a live market snapshot fetch (yfinance-backed) via the Modal '
  'compute-fetch endpoint. Pair with rpc_compute_collect(request_id) to '
  'retrieve a dict with market_cap_usd, adv_usd, valuation_cushion_pct, and '
  'source_liveness. Used by signal_resolver for litigation.financial_materiality.';
