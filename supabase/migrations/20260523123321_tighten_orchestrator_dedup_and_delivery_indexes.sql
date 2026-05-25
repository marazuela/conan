-- Tighten orchestrator content dedup + v3 assessment delivery lookup indexes.
--
-- This migration is intentionally additive except for rebuilding the existing
-- pending content-dedup partial index with the expanded routine system trigger
-- set. Manual/operator refresh and backtest runs still bypass content dedup;
-- routine system refreshes should not burn another Tier-1 assessment when the
-- material primary document set is unchanged.

-- ---------------------------------------------------------------------------
-- 1. Expand pending content-dedup to routine system refresh triggers.
-- ---------------------------------------------------------------------------

with ranked_pending as (
  select
    id,
    row_number() over (
      partition by asset_id, document_set_hash
      order by scheduled_at asc nulls last, created_at asc
    ) as rn
  from public.orchestrator_runs
  where status = 'pending'
    and document_set_hash is not null
    and trigger_type in (
      'new_doc',
      'cross_source',
      'market_move',
      'tier2_escalation',
      'catalyst_proximity',
      'aging_recheck',
      'scheduled'
    )
)
update public.orchestrator_runs r
set
  status = 'skipped_dedupe',
  completed_at = coalesce(r.completed_at, now()),
  error_message = coalesce(
    r.error_message,
    'Skipped by 20260523123321_tighten_orchestrator_dedup_and_delivery_indexes: duplicate pending document_set_hash for asset.'
  )
from ranked_pending rp
where r.id = rp.id
  and rp.rn > 1;

drop index if exists public.orchestrator_runs_pending_content_dedup_idx;

create unique index if not exists orchestrator_runs_pending_content_dedup_idx
  on public.orchestrator_runs
    (asset_id, document_set_hash)
  where status = 'pending'
    and document_set_hash is not null
    and trigger_type in (
      'new_doc',
      'cross_source',
      'market_move',
      'tier2_escalation',
      'catalyst_proximity',
      'aging_recheck',
      'scheduled'
    );

comment on index public.orchestrator_runs_pending_content_dedup_idx is
  'Race-safe dedup for content-equivalent pending orchestrator runs. Covers doc-bus and routine system refresh triggers; manual, operator_refresh, and backtest remain explicit bypasses.';

-- ---------------------------------------------------------------------------
-- 2. Support the v3 fanout asset-recipient prior-email gate.
-- ---------------------------------------------------------------------------

create index if not exists convergence_assessments_asset_created_email_gate_idx
  on public.convergence_assessments (asset_id, created_at desc)
  where band = 'immediate';

comment on index public.convergence_assessments_asset_created_email_gate_idx is
  'Supports fanout prior-email lookup for same-asset immediate assessments before applying recipient-level delivery intersection.';

create index if not exists alert_deliveries_assessment_target_email_gate_idx
  on public.alert_deliveries (target, channel, assessment_id, created_at desc)
  where assessment_id is not null
    and status in ('queued', 'sent');

comment on index public.alert_deliveries_assessment_target_email_gate_idx is
  'Supports v3 assessment email cooldown/material-change gate by recipient and assessment_id.';
