-- v3 over-firing fix PR-B: gate v3-catalyst-proximity-sweep on the
-- orchestrator_enqueue_guard policy function (introduced in
-- 20260612000050).
--
-- Combines two predicates per asset:
--   - 24h "any-trigger" gate (preserved from 20260527000000) — coarse
--     temporal floor that survives even if the hash function returns
--     NULL.
--   - orchestrator_enqueue_guard(asset_id, 'catalyst_proximity', hash) —
--     fine-grained content-aware skip: same-hash assessment <6h old, or
--     a same-hash pending row already queued by another trigger.
--
-- The cron is re-scheduled (drop + re-create); rollback re-applies
-- 20260612000040.

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
      with candidate as (
        select fa.id as asset_id,
               public.compute_document_set_hash_sql(fa.id) as doc_set_hash
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
      ), gated as (
        select c.asset_id,
               c.doc_set_hash,
               public.orchestrator_enqueue_guard(
                 c.asset_id,
                 'catalyst_proximity',
                 c.doc_set_hash
               ) as guard_result
          from candidate c
      )
      insert into public.orchestrator_runs
        (asset_id, trigger_type, tier, status, scheduled_at, document_set_hash)
      select g.asset_id,
             'catalyst_proximity',
             1,
             'pending',
             now(),
             g.doc_set_hash
        from gated g
       where coalesce((g.guard_result->>'skip')::boolean, false) = false
      on conflict (asset_id, trigger_type,
                   coalesce(trigger_doc_id,
                            '00000000-0000-0000-0000-000000000000'::uuid))
        where status = 'pending' do nothing;
    $cron$
  );
end $$;

comment on extension pg_cron is
  'v3-catalyst-proximity-sweep re-scheduled 2026-05-25 (PR-B) to consult '
  'public.orchestrator_enqueue_guard before inserting. Adds content-aware '
  '6h cool-down across triggers, complementing the existing 24h '
  '"any-trigger" gate and the partial unique content-dedup index '
  '(orchestrator_runs_pending_content_dedup_idx).';
