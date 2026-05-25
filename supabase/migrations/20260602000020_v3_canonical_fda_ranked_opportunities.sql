-- ============================================================================
-- Canonical v3 FDA ranking surface
--
-- Problem: legacy candidate scores can outlive the v3 FDA asset lifecycle. When
-- inactive assets inherit candidate context, stale v2 scores can look like live
-- ranked opportunities.
--
-- Fix:
--   1. Keep v_latest_assessments_by_asset as an asset directory view, but only
--      attach candidate context to active FDA assets.
--   2. Add v_fda_ranked_opportunities as the dashboard ranking contract. Its
--      ranking_score is exclusively v3 convergence_assessments output.
-- ============================================================================

create or replace view public.v_latest_assessments_by_asset
with (security_invoker = true) as
with latest_assessment as (
    select distinct on (asset_id)
        ca.asset_id,
        ca.id as latest_assessment_id,
        ca.tier,
        ca.band,
        ca.thesis_direction,
        ca.thesis_summary,
        ca.conviction_pct_calibrated,
        ca.conviction_pct,
        ca.raw_conviction_pct,
        ca.ensemble_dispersion,
        ca.expected_value_bps,
        ca.market_implied_move,
        ca.options_iv,
        ca.constitutional_pass,
        ca.calibration_curve_version,
        ca.cost_usd as latest_assessment_cost_usd,
        ca.latency_ms as latest_assessment_latency_ms,
        ca.created_at as latest_assessment_at
    from public.convergence_assessments ca
    where ca.superseded_at is null
    order by asset_id, created_at desc
),
latest_run as (
    select distinct on (asset_id)
        orun.asset_id,
        orun.id as latest_run_id,
        orun.status as latest_run_status,
        orun.tier as latest_run_tier,
        orun.trigger_type as latest_run_trigger,
        orun.scheduled_at as latest_run_scheduled_at,
        orun.started_at as latest_run_started_at,
        orun.completed_at as latest_run_completed_at,
        orun.cost_actual_usd as latest_run_cost_usd,
        orun.cost_estimate_usd as latest_run_cost_estimate_usd,
        orun.error_message as latest_run_error,
        extract(epoch from (orun.completed_at - orun.started_at))::int * 1000 as latest_run_latency_ms
    from public.orchestrator_runs orun
    order by asset_id, created_at desc
),
runs_30d as (
    select
        asset_id,
        count(*) as runs_30d_count,
        coalesce(sum(cost_actual_usd), 0)::numeric(10,2) as cost_30d_usd,
        count(*) filter (where status = 'failed') as runs_30d_failed
    from public.orchestrator_runs
    where created_at > now() - interval '30 days'
    group by asset_id
),
next_catalyst as (
    select distinct on (asset_id)
        e.asset_id,
        e.id as next_event_id,
        e.event_type as next_event_type,
        e.event_date as next_event_date,
        e.event_status as next_event_status
    from public.fda_regulatory_events e
    where e.event_status = 'pending'
      and e.event_date is not null
      and e.event_date >= current_date
    order by asset_id, event_date asc
),
candidate_link as (
    select distinct on (entity_id)
        entity_id,
        id as candidate_id,
        state as candidate_state,
        current_band as candidate_band,
        current_score as candidate_score,
        next_catalyst_date as candidate_next_catalyst,
        thesis_approved_at
    from public.candidates
    where state in ('watch', 'active')
    order by entity_id, updated_at desc
)
select
    a.id as asset_id,
    a.ticker,
    a.mic,
    a.entity_id,
    a.drug_name,
    a.generic_name,
    a.application_number,
    a.application_type,
    a.indication,
    a.indication_normalized,
    a.mechanism,
    a.sponsor_name,
    a.program_status,
    a.is_active,
    a.watch_priority,
    a.reference_class_signature,
    -- Candidate context is diagnostic only and is suppressed for inactive
    -- assets so stale v2 scores cannot masquerade as live v3 opportunities.
    c.candidate_id,
    c.candidate_state,
    c.candidate_band,
    c.candidate_score,
    c.thesis_approved_at,
    -- latest assessment
    la.latest_assessment_id,
    la.tier,
    la.band,
    la.thesis_direction,
    la.thesis_summary,
    la.conviction_pct_calibrated,
    la.conviction_pct,
    la.ensemble_dispersion,
    la.expected_value_bps,
    la.constitutional_pass,
    la.latest_assessment_at,
    -- latest run
    lr.latest_run_id,
    lr.latest_run_status,
    lr.latest_run_tier,
    lr.latest_run_trigger,
    lr.latest_run_started_at,
    lr.latest_run_completed_at,
    lr.latest_run_cost_usd,
    lr.latest_run_latency_ms,
    lr.latest_run_error,
    -- 30-day rollup
    coalesce(r30.runs_30d_count, 0) as runs_30d_count,
    coalesce(r30.cost_30d_usd, 0)::numeric(10,2) as cost_30d_usd,
    coalesce(r30.runs_30d_failed, 0) as runs_30d_failed,
    -- next catalyst
    nc.next_event_id,
    nc.next_event_type,
    nc.next_event_date,
    coalesce(nc.next_event_date - current_date, null) as days_to_next_catalyst,
    -- freshness
    extract(epoch from (now() - la.latest_assessment_at))::int / 3600 as hours_since_assessment
from public.fda_assets a
left join latest_assessment la on la.asset_id = a.id
left join latest_run lr on lr.asset_id = a.id
left join runs_30d r30 on r30.asset_id = a.id
left join next_catalyst nc on nc.asset_id = a.id
left join candidate_link c on c.entity_id = a.entity_id and a.is_active = true;

comment on view public.v_latest_assessments_by_asset is
    'Dashboard primary read for /assets and /assets/[id]. One row per fda_assets.id with latest non-superseded assessment, latest orchestrator run, 30-day cost/run rollup, next regulatory catalyst, and candidate linkage only for active assets.';


create or replace view public.v_fda_ranked_opportunities
with (security_invoker = true) as
select
    asset_id,
    ticker,
    mic,
    entity_id,
    drug_name,
    generic_name,
    application_number,
    application_type,
    indication,
    indication_normalized,
    mechanism,
    sponsor_name,
    program_status,
    is_active,
    watch_priority,
    reference_class_signature,
    next_event_id,
    next_event_type,
    next_event_date,
    days_to_next_catalyst,
    latest_assessment_id,
    tier,
    band,
    thesis_direction,
    thesis_summary,
    conviction_pct_calibrated,
    conviction_pct,
    ensemble_dispersion,
    expected_value_bps,
    constitutional_pass,
    latest_assessment_at,
    hours_since_assessment,
    latest_run_id,
    latest_run_status,
    latest_run_tier,
    latest_run_trigger,
    latest_run_started_at,
    latest_run_completed_at,
    latest_run_cost_usd,
    latest_run_latency_ms,
    latest_run_error,
    runs_30d_count,
    cost_30d_usd,
    runs_30d_failed,
    conviction_pct_calibrated as ranking_score,
    case
        when latest_assessment_id is not null
          and latest_assessment_at < now() - interval '7 days'
          then 'stale_assessment'
        when latest_assessment_id is not null
          then 'assessed'
        when latest_run_status in ('pending', 'running')
          then 'assessment_pending'
        when latest_run_status = 'declined'
          then 'pregate_declined'
        when latest_run_status in ('failed', 'failed_constitutional')
          then 'assessment_failed'
        when latest_run_status in ('skipped_dedupe', 'skipped_budget', 'killed_budget')
          then latest_run_status
        else 'unassessed'
    end as ranking_status,
    (latest_assessment_id is not null) as is_rankable,
    'v3_convergence_assessment'::text as score_source,
    candidate_id as legacy_candidate_id,
    candidate_state as legacy_candidate_state,
    candidate_band as legacy_candidate_band,
    candidate_score as legacy_candidate_score,
    thesis_approved_at as legacy_thesis_approved_at
from public.v_latest_assessments_by_asset
where is_active = true;

comment on view public.v_fda_ranked_opportunities is
    'Canonical dashboard ranking surface for FDA opportunities. Includes active fda_assets only; ranking_score is exclusively convergence_assessments.conviction_pct_calibrated. Legacy candidate fields are diagnostics and never drive rank.';
