-- v3 over-firing fix PR-A: stamp document_set_hash on catalyst_proximity
-- pg_cron inserts.
--
-- Problem (observed 14d ending 2026-05-25):
--   • Reactor-driven enqueues (new_doc, cross_source) populate
--     orchestrator_runs.document_set_hash and participate in
--     orchestrator_runs_pending_content_dedup_idx.
--   • The pg_cron catalyst_proximity sweep
--     (20260527000000_v3_catalyst_proximity_sweep.sql) inserts with NULL
--     hash, so it's exempt from the index even though 20260523123321 added
--     catalyst_proximity to the index predicate.
--   • Result: same-asset same-corpus collisions across triggers
--     (VRDN/Veligrotug 41 runs, 149 collisions within 6h windows, ~$32 of
--     wasted Sonnet/Opus calls).
--
-- Fix: re-schedule v3-catalyst-proximity-sweep so the INSERT calls
--   public.compute_document_set_hash_sql(fa.id) and stores it on the row.
--   The 24h "any-trigger" gate is preserved as a complementary safeguard
--   for the (rare) case where an asset has zero material primary docs and
--   the hash function returns NULL.
--
-- Rollback: re-apply 20260527000000 to restore the prior cron body.

create extension if not exists pg_cron with schema extensions cascade;

do $$
declare
  v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-catalyst-proximity-sweep';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-catalyst-proximity-sweep',
    '0 */4 * * *',
    $cron$
      insert into public.orchestrator_runs
        (asset_id, trigger_type, tier, status, scheduled_at, document_set_hash)
      select fa.id,
             'catalyst_proximity',
             1,
             'pending',
             now(),
             public.compute_document_set_hash_sql(fa.id)
        from public.fda_assets fa
       where fa.is_active = true
         and fa.aging_state in ('active','watch')
         and fa.next_catalyst_date is not null
         and fa.next_catalyst_date
             between (now())::date and (now() + interval '90 days')::date
         and not exists (
           select 1 from public.orchestrator_runs orun
            where orun.asset_id = fa.id
              and orun.created_at > now() - interval '24 hours'
         )
      on conflict (asset_id, trigger_type,
                   coalesce(trigger_doc_id,
                            '00000000-0000-0000-0000-000000000000'::uuid))
        where status = 'pending' do nothing;
    $cron$
  );
end $$;

comment on extension pg_cron is
  'v3-catalyst-proximity-sweep re-scheduled 2026-05-25 to stamp '
  'document_set_hash on every insert so the partial unique content-dedup '
  'index (orchestrator_runs_pending_content_dedup_idx, see migration '
  '20260523123321) actually applies to proximity enqueues. Prior body in '
  'migration 20260527000000 wrote NULL hashes which the index ignored.';
