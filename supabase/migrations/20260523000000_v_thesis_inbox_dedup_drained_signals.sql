-- ============================================================================
-- v_thesis_inbox: dedup signals already covered by a thesis_job
--
-- Background: as originally defined in 20260522000000_v3_dashboard_views.sql,
-- section 1 of v_thesis_inbox surfaces every fresh signal whose
-- band_with_bonus (or band) is immediate/watchlist, with no exclusion for
-- signals that already have a thesis_job. The thesis_writer / AI rescorer
-- never updates signals.band_with_bonus, so once a job reaches a terminal
-- status (completed/failed/gate_failed then resolved, or v2-vocabulary
-- scoring_complete_below_immediate / promoted / resolved DLQ), the raw
-- signal row leaks back into the inbox alongside (or after) the thesis_job
-- row that was the actual unit of work.
--
-- This was first identified and fixed inside dashboard's lib/workspace.ts
-- (conan-dashboard PR #5), but that file was deleted in dashboard PR #7
-- ("Phase D: delete v2 surfaces"). The fix now lives at the view level
-- so every consumer of v_thesis_inbox benefits and the dashboard does not
-- need a per-page-load round-trip.
--
-- Fix: section 1 gains a `NOT EXISTS (thesis_jobs for this signal)` clause.
-- Any thesis_job for the signal is either (a) already rendered by section 2
-- (active/under review) or (b) drained by the AI rescorer; either way the
-- raw signal is redundant. The predicate is intentionally
-- status-vocabulary-agnostic (no enum list), to survive v2/v3 status drift.
-- ============================================================================

create or replace view public.v_thesis_inbox
with (security_invoker = true) as
-- 1) Fresh signals (last 7 days, band immediate/watchlist),
--    excluding signals already covered by a thesis_job (active or drained).
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
  and not exists (
      select 1
      from public.thesis_jobs tj
      where tj.signal_id = s.signal_id
  )

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
    'Dashboard primary read for /. Unified triage inbox across signals (7d, immediate/watchlist, excluding signals that already have a thesis_job), open thesis_jobs, open aging failures, and critical/high operator_flags. Each row carries a uid like "sig:..."/"thesis:..."/"aging:..."/"flag:..." for stable identity. Section 1 excludes signals with any thesis_job (active or drained) to prevent rescored signals from leaking back after the AI rescorer marks them terminal.';
