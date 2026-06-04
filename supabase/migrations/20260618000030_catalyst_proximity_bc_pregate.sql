-- Extend the binary-catalyst pre-gate ("NDA filter") to the catalyst_proximity
-- dispatch path so the gate is enforced on EVERY live enqueue, not just the
-- reactor's document path.
--
-- The sweep enqueues by asset (not by document), so it scores via the SQL mirror
-- public.bc_pregate_score_sql(asset_id) rather than the TS reactor. Behavior:
--   * bc_pregate_enabled='false' (shadow): unchanged — every candidate -> 'pending',
--     but bc_pregate_score/_inputs are now recorded for offline measurement.
--   * bc_pregate_enabled='true' (active): assets below threshold get a status
--     'declined' audit row (routine_declined=true + decline_reasons) instead of
--     'pending', mirroring the reactor. The existing 24h not-exists guard caps this
--     at one row per asset per day, so declined rows do not pile up every sweep.
--
-- manual / tier2_escalation paths stay exempt (operator/escalation re-runs);
-- 'scheduled' has no live enqueue code.
--
-- cron.schedule() upserts by name, so this replaces the command on the existing
-- v3-catalyst-proximity-sweep job (every 4 hours).

SELECT cron.schedule(
  'v3-catalyst-proximity-sweep',
  '0 */4 * * *',
  $cmd$
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
                 c.asset_id, 'catalyst_proximity', c.doc_set_hash
               ) as guard_result,
               public.bc_pregate_score_sql(c.asset_id) as pregate,
               coalesce(
                 btrim((select value from public.internal_config
                          where key = 'bc_pregate_enabled')) ~* '^(true|1|yes)$',
                 false
               ) as pregate_enabled
          from candidate c
      ), decided as (
        select g.*,
               (g.pregate_enabled
                and not coalesce((g.pregate->>'passed')::boolean, false)) as is_declined
          from gated g
         where coalesce((g.guard_result->>'skip')::boolean, false) = false
      )
      insert into public.orchestrator_runs
        (asset_id, trigger_type, tier, status, scheduled_at, document_set_hash,
         bc_pregate_score, bc_pregate_inputs, routine_declined, decline_reasons)
      select d.asset_id,
             'catalyst_proximity',
             1,
             case when d.is_declined then 'declined' else 'pending' end,
             now(),
             d.doc_set_hash,
             (d.pregate->>'score')::numeric,
             d.pregate,
             d.is_declined,
             case when d.is_declined
                  then array(select jsonb_array_elements_text(d.pregate->'reasons'))
                  else null end
        from decided d
      on conflict (asset_id, trigger_type,
                   coalesce(trigger_doc_id,
                            '00000000-0000-0000-0000-000000000000'::uuid))
        where status = 'pending' do nothing;
  $cmd$
);
