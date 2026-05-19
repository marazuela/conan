-- v3 catalyst-proximity sweeper — PR-3 of the cross-cutting orchestrator fix.
--
-- Problem: the v3 orchestrator is purely document-triggered. asset_linker
-- attaches a doc to an asset → reactor enqueues orchestrator_runs. Assets
-- with empty doc inboxes never get scored. As of 2026-05-14 the live DB has
-- 1 fda_asset in aging_state='active' (VERA, PDUFA 2026-07-07) with 0
-- asset_documents and 0 orchestrator_runs. The autonomous orchestrator has
-- never assessed it despite the catalyst being 55 days out.
--
-- Fix: every 4 hours, scan fda_assets for active/watch assets within 90 days
-- of a known catalyst date and enqueue a tier-1 orchestrator_runs row if one
-- hasn't been enqueued in the last 24h (for ANY trigger_type — a doc-driven
-- run an hour ago suppresses a redundant proximity enqueue).
--
-- Direct pg_cron insert (no Modal hop). The drain loop in orchestrator_app.py
-- (v3-orchestrator-drain, */5 * * * *) picks pending rows up on its own.
-- Saves a _conan_modal_post_enqueue round-trip + compute-secret dependency.
--
-- Rollback: select cron.unschedule('v3-catalyst-proximity-sweep');
--           ALTER TABLE orchestrator_runs DROP CONSTRAINT orchestrator_runs_trigger_type_check;
--           -- (then re-add without 'catalyst_proximity')
--
-- Sequencing: this is PR-3 of a 5-PR cross-cutting fix. Lands first because
-- it can only add pending rows (the drain rate-limits via thesis_daily_cap).

-- ---------------------------------------------------------------------------
-- 1. Extend trigger_type CHECK to allow 'catalyst_proximity' on both
--    orchestrator_runs and convergence_assessments.
-- ---------------------------------------------------------------------------
-- The live DB includes 'aging_recheck' (hot-added previously) which the
-- in-repo migrations don't show. We rebuild the constraint with the full
-- observed enum + the new value.

alter table public.orchestrator_runs
  drop constraint if exists orchestrator_runs_trigger_type_check;

alter table public.orchestrator_runs
  add constraint orchestrator_runs_trigger_type_check
  check (trigger_type in (
    'new_doc', 'cross_source', 'scheduled', 'operator_refresh',
    'market_move', 'tier2_escalation', 'backtest', 'manual',
    'aging_recheck', 'catalyst_proximity'
  ));

alter table public.convergence_assessments
  drop constraint if exists convergence_assessments_trigger_type_check;

alter table public.convergence_assessments
  add constraint convergence_assessments_trigger_type_check
  check (trigger_type in (
    'new_doc', 'cross_source', 'scheduled', 'operator_refresh',
    'market_move', 'tier2_escalation', 'backtest', 'manual',
    'aging_recheck', 'catalyst_proximity'
  ));

-- ---------------------------------------------------------------------------
-- 2. Register the sweeper as a pg_cron job.
-- ---------------------------------------------------------------------------

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
        (asset_id, trigger_type, tier, status, scheduled_at)
      select fa.id, 'catalyst_proximity', 1, 'pending', now()
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
      -- Standalone unique index (not a named constraint), so use the
      -- column-list inference form. PG matches the partial unique index by
      -- the WHERE clause being a subset of `status = 'pending'`.
      on conflict (asset_id, trigger_type,
                   coalesce(trigger_doc_id,
                            '00000000-0000-0000-0000-000000000000'::uuid))
        where status = 'pending' do nothing;
    $cron$
  );
end $$;

comment on extension pg_cron is
  'v3-catalyst-proximity-sweep added 2026-05-14: scans fda_assets every 4h for active/watch assets within 90d of catalyst and enqueues a tier-1 orchestrator_runs row if none in last 24h.';
