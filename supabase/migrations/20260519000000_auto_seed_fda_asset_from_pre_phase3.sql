-- Auto-seed fda_assets stubs from pre_phase3_readout binary_catalyst signals.
--
-- Problem: pre_phase3_readout_scanner emits binary_catalyst signals for
-- Phase-3 trials whose primary completion date is within [T-14d, T+90d].
-- These tickers (~70 distinct entities in last 30 days as of 2026-05-11)
-- have no NDA/BLA number yet — the watchlist backfill (`fda_backfill_watchlist`)
-- only covers entities with a filed application. Result: the v3 asset_linker
-- cron has no fda_asset to attach docs to, so the binary_catalyst signal fires
-- but the convergence pipeline never engages.
--
-- Fix: after a pre_phase3_readout signal lands with a resolved public-issuer
-- ticker (`raw_payload->'auto_seed_fda_asset'` set by the Python side) and
-- the entity has no existing fda_asset, insert a stub row:
--   - program_status = 'phase3' (consistent with the trial phase)
--   - is_active = true (so asset_linker_run default scope picks it up)
--   - application_number = '' (default; no filed application yet)
--   - drug_name extracted from the trial's lead drug intervention
--
-- Idempotency: ON CONFLICT (ticker, drug_name, application_number) DO NOTHING
-- against the existing UNIQUE constraint. Plus a coarse "entity already has
-- some fda_asset" gate so re-running a scan on the same trial after a manual
-- watchlist add is a no-op.
--
-- Scope: trigger only fires for scoring_profile='binary_catalyst' AND
-- signal_type='pre_phase3_readout'. Every other signal type is short-circuited
-- on the first IF check — overhead is one nullable jsonb fetch.
--
-- Rollback:
--   DROP TRIGGER IF EXISTS auto_seed_fda_asset_from_signal_tg ON public.signals;
--   DROP FUNCTION IF EXISTS public.auto_seed_fda_asset_from_signal();

create or replace function public.auto_seed_fda_asset_from_signal()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $func$
declare
  v_hint jsonb;
  v_ticker text;
  v_drug_name text;
  v_sponsor text;
  v_indication text;
  v_nct text;
  v_pcd text;
begin
  if new.scoring_profile is distinct from 'binary_catalyst' then
    return new;
  end if;
  if new.signal_type is distinct from 'pre_phase3_readout' then
    return new;
  end if;
  if new.entity_id is null then
    return new;
  end if;

  v_hint := new.raw_payload -> 'auto_seed_fda_asset';
  if v_hint is null then
    return new;
  end if;

  v_ticker := nullif(v_hint ->> 'ticker', '');
  v_drug_name := nullif(v_hint ->> 'drug_name', '');
  v_sponsor := nullif(v_hint ->> 'sponsor_name', '');
  v_indication := nullif(v_hint ->> 'indication', '');
  v_nct := nullif(v_hint ->> 'nct_id', '');
  v_pcd := nullif(v_hint ->> 'primary_completion_date', '');

  if v_ticker is null or v_drug_name is null then
    return new;
  end if;

  -- Don't double-seed when the entity already has any fda_asset. A manual
  -- watchlist add or a prior auto-seed both block further stubs.
  if exists (select 1 from public.fda_assets where entity_id = new.entity_id) then
    return new;
  end if;

  insert into public.fda_assets (
    ticker, drug_name, application_number,
    entity_id, sponsor_name, indication, program_status,
    is_active, watch_priority,
    extensions
  )
  values (
    v_ticker, v_drug_name, '',
    new.entity_id, v_sponsor, v_indication, 'phase3',
    true, 3,
    jsonb_build_object(
      'auto_seeded_from', 'pre_phase3_readout_scanner',
      'seeding_signal_id', new.signal_id,
      'nct_id', v_nct,
      'primary_completion_date', v_pcd,
      'seeded_at', now()
    )
  )
  on conflict (ticker, drug_name, application_number) do nothing;

  return new;
end;
$func$;

drop trigger if exists auto_seed_fda_asset_from_signal_tg on public.signals;
create trigger auto_seed_fda_asset_from_signal_tg
  after insert on public.signals
  for each row
  execute function public.auto_seed_fda_asset_from_signal();
