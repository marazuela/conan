-- Phase C: explicit prediction targets + Stage 9 signature dedup.
--
-- Additive first: runtime can begin writing these fields before any NOT NULL
-- constraint is considered. Historical rows are backfilled to the current
-- forward-return convention used by label_forward_returns.py.

begin;

alter table public.convergence_assessments
  add column if not exists target_type text,
  add column if not exists horizon_days integer,
  add column if not exists event_anchor text,
  add column if not exists label_rule text,
  add column if not exists convergence_signature text;

alter table public.eval_harness
  add column if not exists target_type text,
  add column if not exists horizon_days integer,
  add column if not exists event_anchor text,
  add column if not exists label_rule text;

update public.convergence_assessments
set
  target_type = coalesce(target_type, 'price_move'),
  horizon_days = coalesce(horizon_days, 30),
  label_rule = coalesce(label_rule, 'forward_return_t30_calendar')
where target_type is null
   or horizon_days is null
   or label_rule is null;

update public.eval_harness
set
  target_type = coalesce(target_type, 'price_move'),
  horizon_days = coalesce(
    horizon_days,
    case
      when (realized_outcome_data->>'hit_window_days') ~ '^[0-9]+$'
      then nullif((realized_outcome_data->>'hit_window_days')::integer, 0)
      else null
    end,
    30
  ),
  label_rule = coalesce(
    label_rule,
    realized_outcome_data->>'label_rule',
    'forward_return_t30_calendar'
  ),
  event_anchor = coalesce(event_anchor, realized_outcome_data->>'event_id')
where target_type is null
   or horizon_days is null
   or label_rule is null
   or event_anchor is null;

alter table public.convergence_assessments
  drop constraint if exists convergence_assessments_target_type_check,
  add constraint convergence_assessments_target_type_check
  check (
    target_type is null
    or target_type in ('price_move', 'regulatory_outcome', 'event_outcome')
  );

alter table public.convergence_assessments
  drop constraint if exists convergence_assessments_label_rule_check,
  add constraint convergence_assessments_label_rule_check
  check (
    label_rule is null
    or label_rule in (
      'forward_return_t30_calendar',
      'forward_return',
      'approval_decision',
      'adcom_recommendation'
    )
  );

alter table public.convergence_assessments
  drop constraint if exists convergence_assessments_horizon_days_check,
  add constraint convergence_assessments_horizon_days_check
  check (horizon_days is null or horizon_days > 0);

alter table public.eval_harness
  drop constraint if exists eval_harness_target_type_check,
  add constraint eval_harness_target_type_check
  check (
    target_type is null
    or target_type in ('price_move', 'regulatory_outcome', 'event_outcome')
  );

alter table public.eval_harness
  drop constraint if exists eval_harness_label_rule_check,
  add constraint eval_harness_label_rule_check
  check (
    label_rule is null
    or label_rule in (
      'forward_return_t30_calendar',
      'forward_return',
      'approval_decision',
      'adcom_recommendation'
    )
  );

alter table public.eval_harness
  drop constraint if exists eval_harness_horizon_days_check,
  add constraint eval_harness_horizon_days_check
  check (horizon_days is null or horizon_days > 0);

create unique index if not exists convergence_assessments_active_signature_idx
  on public.convergence_assessments (asset_id, convergence_signature)
  where convergence_signature is not null
    and superseded_by is null;

create index if not exists convergence_assessments_prediction_target_idx
  on public.convergence_assessments (target_type, label_rule, horizon_days);

create index if not exists eval_harness_prediction_target_idx
  on public.eval_harness (target_type, label_rule, horizon_days);

comment on column public.convergence_assessments.target_type is
  'Phase C.2 prediction target family: price_move, regulatory_outcome, or event_outcome.';
comment on column public.convergence_assessments.horizon_days is
  'Phase C.2 prediction horizon in days for price_move targets; null for event-anchored targets.';
comment on column public.convergence_assessments.event_anchor is
  'Phase C.2 catalyst/event anchor when the prediction is event-specific.';
comment on column public.convergence_assessments.label_rule is
  'Phase C.2 realized-outcome labeling rule used by eval/calibration dispatch.';
comment on column public.convergence_assessments.convergence_signature is
  'Phase C.5 md5 signature over normalized Stage 9 output for duplicate active-row suppression.';

commit;
