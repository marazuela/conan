-- v3 dashboard inbox view: sunset candidates.current_band reads — PR-4 of the
-- cross-cutting orchestrator fix.
--
-- Problem: AXSM was approved 2026-05-06 and v3 correctly drained it
-- (fda_assets.aging_state='expired', is_active=false; convergence_assessments
-- superseded_at populated). But the dashboard still shows AXSM at IMMEDIATE
-- band because v_thesis_inbox sections 2 (open thesis_jobs) and 3 (open aging
-- failures) read `cand.current_band` from the v2 candidates table — and
-- nothing in the v3 drain ever writes back to that column. The legacy column
-- is a stale shadow of v3 truth.
--
-- Fix: replace `cand.current_band::text as band` in sections 2 and 3 with
-- COALESCE(la.band, cand.current_band)::text, where `la` is the latest
-- non-superseded convergence_assessments row for the candidate's asset. v3
-- truth wins; the v2 column survives as fallback only for candidates with
-- zero v3 assessments (the cold-start window during PR-3 sweeper rollout).
--
-- Sections 1 (signals) and 4 (operator_flags) are unchanged — they don't
-- read candidates.current_band.
--
-- Rollback: restore the migration body from
-- 20260523000000_v_thesis_inbox_dedup_drained_signals.sql via `create or replace view`.
--
-- Sequencing: PR-4 of 5. Independent of PR-1/2/3; can land in any order
-- after the v3 schema (convergence_assessments) exists.

create or replace view public.v_thesis_inbox
with (security_invoker = true) as
-- 1) Fresh signals (last 7 days, band immediate/watchlist), excluding
--    signals already covered by a thesis_job (active or drained).
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
-- PR-4: band = latest non-superseded v3 convergence_assessments.band, with
-- v2 cand.current_band as fallback only when no v3 row exists for the asset.
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
    coalesce(la_tj.band::text, cand.current_band::text) as band,
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
left join lateral (
    select ca.band
      from public.convergence_assessments ca
     where ca.asset_id = fa.id and ca.superseded_at is null
     order by ca.created_at desc
     limit 1
) la_tj on true
where tj.resolved_at is null
  and tj.status in ('completed', 'failed', 'gate_failed', 'running', 'pending')

union all

-- 3) Open aging failures ----------------------------------------------------
-- PR-4: same v3-band-first coalesce as section 2.
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
    coalesce(la_af.band::text, cand.current_band::text) as band,
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
left join lateral (
    select ca.band
      from public.convergence_assessments ca
     where ca.asset_id = fa.id and ca.superseded_at is null
     order by ca.created_at desc
     limit 1
) la_af on true
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
    'Dashboard primary read for /. PR-4 (2026-05-14): sections 2 and 3 now COALESCE the latest non-superseded convergence_assessments.band over the legacy candidates.current_band, so v3 drain results land on the dashboard without write-back to the v2 candidates table. Cold-start fallback: assets with zero v3 assessments still surface via cand.current_band. See plan-this-for-optimal-idempotent-kettle.md PR-4.';
