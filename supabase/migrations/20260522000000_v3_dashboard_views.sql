-- ============================================================================
-- v3 Dashboard Views — Phase A of the final UI upgrade
--
-- Four read-only views that become the dashboard's primary data surfaces.
-- All use SECURITY INVOKER so they inherit RLS from the underlying tables.
--
-- Built against the live schema as of 2026-05-11. Columns reflect what
-- information_schema.columns actually returns, not local migration files.
--
-- Views:
--   v_latest_assessments_by_asset  — one row per FDA asset, latest run/assessment
--   v_open_operator_flags          — open flags with denormalized context
--   v_thesis_inbox                 — unified triage inbox (4 sources → 1 shape)
--   v_assessment_stage_chain       — stage timing per run, with sub-agent aggregates
-- ============================================================================

-- ----------------------------------------------------------------------------
-- v_latest_assessments_by_asset
-- ----------------------------------------------------------------------------
-- Powers: /assets directory, /assets/[id] header, inbox asset rows, op KPI strip.
-- One row per fda_assets.id, with latest non-superseded assessment + run.
-- ----------------------------------------------------------------------------
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
    -- candidate context (when entity is tracked as candidate)
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
left join candidate_link c on c.entity_id = a.entity_id;

comment on view public.v_latest_assessments_by_asset is
    'Dashboard primary read for /assets and /assets/[id]. One row per fda_assets.id with latest non-superseded assessment, latest orchestrator run, 30-day cost/run rollup, next regulatory catalyst, and candidate linkage when applicable.';


-- ----------------------------------------------------------------------------
-- v_open_operator_flags
-- ----------------------------------------------------------------------------
-- Powers: /operator/flags inbox, operator sidebar count, /operator KPI strip.
-- One row per open flag with denormalized entity context and resolve hint.
-- ----------------------------------------------------------------------------
create or replace view public.v_open_operator_flags
with (security_invoker = true) as
select
    f.id as flag_id,
    f.severity,
    f.source,
    f.kind,
    f.title,
    f.body,
    f.evidence,
    f.created_at,
    f.updated_at,
    extract(epoch from (now() - f.created_at)) / 3600.0 as age_hours,
    -- entity references
    f.scanner_id,
    s.name as scanner_name,
    s.status as scanner_status,
    f.entity_id,
    f.signal_id,
    f.candidate_id,
    cand.ticker as candidate_ticker,
    cand.state as candidate_state,
    -- best-effort asset linkage via candidate.entity_id
    fa.id as asset_id,
    fa.ticker as asset_ticker,
    fa.drug_name as asset_drug_name,
    -- resolve hint derived from source
    case f.source
        when 'thesis_jobs' then 'requeue_thesis'
        when 'orchestrator_cost' then 'review_run_cost'
        when 'orchestrator_timeout' then 'retry_run'
        when 'asset_linker' then 'review_asset_linker'
        when 'scanner_health' then 'check_scanner'
        when 'eval' then 'review_eval_case'
        when 'calibration' then 'trigger_calibration_refit'
        when 'dlq' then 'inspect_dlq'
        when 'manual' then 'mark_seen'
        else 'mark_seen'
    end as resolve_action_hint
from public.operator_flags f
left join public.scanners s on s.id = f.scanner_id
left join public.candidates cand on cand.id = f.candidate_id
left join public.fda_assets fa on fa.entity_id = coalesce(cand.entity_id, f.entity_id)
where f.resolved_at is null;

comment on view public.v_open_operator_flags is
    'Dashboard primary read for /operator/flags. Open flags only, with denormalized scanner/candidate/asset context, age_hours, and a resolve_action_hint derived from source.';


-- ----------------------------------------------------------------------------
-- v_thesis_inbox
-- ----------------------------------------------------------------------------
-- Powers: / (triage inbox). Unifies 4 sources into one typed row shape.
-- Sources: signals, thesis_jobs, candidate_aging_failures, operator_flags.
-- Filters: each source pre-filters to its own "needs attention" predicate.
-- ----------------------------------------------------------------------------
create or replace view public.v_thesis_inbox
with (security_invoker = true) as
-- 1) Fresh signals (last 7 days, band immediate/watchlist) ------------------
select
    'sig:' || s.signal_id as uid,
    'signal'::text as kind,
    s.created_at,
    s.created_at as updated_at,
    case
        when s.band_with_bonus = 'immediate' then 4
        when s.band_with_bonus = 'watchlist' then 3
        when s.band = 'immediate' then 4
        when s.band = 'watchlist' then 3
        else 2
    end as priority,
    s.entity_id,
    fa.id as asset_id,
    fa.ticker as asset_ticker,
    fa.drug_name as asset_drug_name,
    coalesce(s.scoring_profile, 'unknown') as profile,
    coalesce(s.band_with_bonus::text, s.band::text) as band,
    null::int as tier,
    null::numeric as conviction,
    s.score_with_bonus as score,
    s.scanner_id,
    null::uuid as run_id,
    s.signal_id as signal_id_text,
    null::uuid as thesis_job_id,
    null::uuid as flag_id,
    null::uuid as aging_failure_id,
    null::uuid as candidate_id,
    coalesce(fa.ticker, '?') ||
        ' · ' ||
        coalesce(s.signal_type, 'signal') ||
        coalesce(' · ' || s.thesis_direction, '') as title,
    coalesce(fa.drug_name, '') as subtitle,
    null::text as aging_reason,
    null::text as flag_severity,
    (coalesce(s.band_with_bonus::text, s.band::text) in ('immediate', 'watchlist')) as is_action_required,
    null::timestamptz as removed_at
from public.signals s
left join public.fda_assets fa on fa.entity_id = s.entity_id
where s.created_at > now() - interval '7 days'
  and coalesce(s.band_with_bonus::text, s.band::text) in ('immediate', 'watchlist')

union all

-- 2) Open thesis jobs (status completed or running, not yet resolved) -------
select
    'thesis:' || tj.id::text as uid,
    'thesis_job'::text as kind,
    tj.created_at,
    tj.updated_at,
    case
        when tj.status = 'failed' then 3
        when tj.status = 'completed' then 3
        when tj.status = 'gate_failed' then 2
        else 2
    end as priority,
    cand.entity_id,
    fa.id as asset_id,
    coalesce(fa.ticker, cand.ticker) as asset_ticker,
    fa.drug_name as asset_drug_name,
    cand.scoring_profile as profile,
    cand.current_band::text as band,
    null::int as tier,
    null::numeric as conviction,
    cand.current_score as score,
    null::uuid as scanner_id,
    null::uuid as run_id,
    tj.signal_id as signal_id_text,
    tj.id as thesis_job_id,
    null::uuid as flag_id,
    null::uuid as aging_failure_id,
    tj.candidate_id,
    coalesce(fa.ticker, cand.ticker, '?') ||
        ' · thesis ' || tj.status as title,
    case
        when tj.status = 'failed' then 'attempt ' || tj.attempt_count::text || ' failed'
        when tj.status = 'completed' then 'ready for review'
        when tj.status = 'gate_failed' then array_to_string(tj.gate_reasons, ', ')
        else tj.status
    end as subtitle,
    null::text as aging_reason,
    null::text as flag_severity,
    (tj.status in ('completed', 'failed', 'gate_failed')) as is_action_required,
    tj.resolved_at as removed_at
from public.thesis_jobs tj
left join public.candidates cand on cand.id = tj.candidate_id
left join public.fda_assets fa on fa.entity_id = cand.entity_id
where tj.resolved_at is null
  and tj.status in ('completed', 'failed', 'gate_failed', 'running', 'pending')

union all

-- 3) Open aging failures ----------------------------------------------------
select
    'aging:' || af.id::text as uid,
    'aging_failure'::text as kind,
    af.attempt_at as created_at,
    af.attempt_at as updated_at,
    case
        when af.consecutive_failures >= 3 then 3
        else 2
    end as priority,
    cand.entity_id,
    fa.id as asset_id,
    coalesce(fa.ticker, cand.ticker) as asset_ticker,
    fa.drug_name as asset_drug_name,
    cand.scoring_profile as profile,
    cand.current_band::text as band,
    null::int as tier,
    null::numeric as conviction,
    cand.current_score as score,
    null::uuid as scanner_id,
    null::uuid as run_id,
    null::text as signal_id_text,
    null::uuid as thesis_job_id,
    null::uuid as flag_id,
    af.id as aging_failure_id,
    af.candidate_id,
    coalesce(fa.ticker, cand.ticker, '?') ||
        ' · aging failed (' || af.error_kind || ')' as title,
    coalesce(af.error_message, '') as subtitle,
    af.error_kind as aging_reason,
    null::text as flag_severity,
    true as is_action_required,
    af.resolved_at as removed_at
from public.candidate_aging_failures af
join public.candidates cand on cand.id = af.candidate_id
left join public.fda_assets fa on fa.entity_id = cand.entity_id
where af.resolved_at is null

union all

-- 4) Critical/high open operator flags --------------------------------------
select
    'flag:' || f.id::text as uid,
    'flag'::text as kind,
    f.created_at,
    f.updated_at,
    case f.severity
        when 'critical' then 4
        when 'high' then 3
        when 'warn' then 2
        else 1
    end as priority,
    f.entity_id,
    fa.id as asset_id,
    coalesce(fa.ticker, cand.ticker) as asset_ticker,
    fa.drug_name as asset_drug_name,
    null::text as profile,
    null::text as band,
    null::int as tier,
    null::numeric as conviction,
    null::numeric as score,
    f.scanner_id,
    null::uuid as run_id,
    f.signal_id as signal_id_text,
    null::uuid as thesis_job_id,
    f.id as flag_id,
    null::uuid as aging_failure_id,
    f.candidate_id,
    coalesce(fa.ticker, cand.ticker, f.source) ||
        ' · ' || f.severity || ' · ' || f.title as title,
    coalesce(f.body, '') as subtitle,
    null::text as aging_reason,
    f.severity as flag_severity,
    (f.severity in ('critical', 'high')) as is_action_required,
    f.resolved_at as removed_at
from public.operator_flags f
left join public.candidates cand on cand.id = f.candidate_id
left join public.fda_assets fa on fa.entity_id = coalesce(cand.entity_id, f.entity_id)
where f.resolved_at is null
  and f.severity in ('critical', 'high');

comment on view public.v_thesis_inbox is
    'Dashboard primary read for /. Unified triage inbox across signals (7d, immediate/watchlist), open thesis_jobs, open aging failures, and critical/high operator_flags. Each row carries a uid like "sig:..."/"thesis:..."/"aging:..."/"flag:..." for stable identity.';


-- ----------------------------------------------------------------------------
-- v_assessment_stage_chain
-- ----------------------------------------------------------------------------
-- Powers: /assets/[id]/assessment/[runId] and /operator/runs/[id].
-- One row per stage of a run, ordered by created_at, with sub-agent aggregates
-- for the same assessment.
-- ----------------------------------------------------------------------------
create or replace view public.v_assessment_stage_chain
with (security_invoker = true) as
with sub_agent_rollup as (
    select
        assessment_id,
        count(*) as sub_agent_count,
        count(*) filter (where schema_pass) as sub_agent_pass_count,
        sum(cost_usd)::numeric(10,4) as sub_agent_cost_usd,
        sum(latency_ms) as sub_agent_total_latency_ms,
        array_agg(id order by created_at) as sub_agent_call_ids,
        array_agg(distinct role) as sub_agent_roles
    from public.sub_agent_calls
    where assessment_id is not null
    group by assessment_id
),
ranked_stages as (
    select
        m.id as stage_metric_id,
        m.assessment_id,
        m.stage_name,
        m.model,
        m.input_tokens,
        m.output_tokens,
        m.thinking_tokens,
        m.cache_read_tokens,
        m.cache_creation_tokens,
        m.cost_usd,
        m.latency_ms,
        m.status as stage_status,
        m.notes,
        m.created_at,
        row_number() over (partition by m.assessment_id order by m.created_at asc) as stage_index
    from public.assessment_stage_metrics m
)
select
    r.id as run_id,
    r.asset_id,
    r.tier as run_tier,
    r.trigger_type as run_trigger,
    r.status as run_status,
    r.scheduled_at,
    r.started_at,
    r.completed_at,
    r.cost_actual_usd as run_cost_usd,
    r.assessment_id,
    rs.stage_index,
    rs.stage_name,
    rs.model,
    rs.input_tokens,
    rs.output_tokens,
    rs.thinking_tokens,
    rs.cache_read_tokens,
    rs.cache_creation_tokens,
    rs.cost_usd as stage_cost_usd,
    rs.latency_ms as stage_latency_ms,
    rs.stage_status,
    rs.notes as stage_notes,
    rs.created_at as stage_at,
    coalesce(sar.sub_agent_count, 0) as sub_agent_count,
    coalesce(sar.sub_agent_pass_count, 0) as sub_agent_pass_count,
    coalesce(sar.sub_agent_cost_usd, 0)::numeric(10,4) as sub_agent_cost_usd,
    coalesce(sar.sub_agent_total_latency_ms, 0) as sub_agent_total_latency_ms,
    sar.sub_agent_call_ids,
    sar.sub_agent_roles
from public.orchestrator_runs r
left join ranked_stages rs on rs.assessment_id = r.assessment_id
left join sub_agent_rollup sar on sar.assessment_id = r.assessment_id;

comment on view public.v_assessment_stage_chain is
    'Dashboard primary read for /assets/[id]/assessment/[runId] and /operator/runs/[id]. One row per (run, stage) ordered by stage_index, joined with sub_agent_calls aggregates for the same assessment. Runs without an assessment_id still appear (single row with NULL stage fields).';


-- ----------------------------------------------------------------------------
-- Smoke queries — run after migration applies, before merging
-- ----------------------------------------------------------------------------
-- select count(*) as assets from public.v_latest_assessments_by_asset;
-- select kind, count(*) from public.v_thesis_inbox group by kind order by 1;
-- select severity, count(*) from public.v_open_operator_flags group by severity;
-- select count(*) as stages_run_pairs from public.v_assessment_stage_chain;
