-- Tighten _v3_pipeline_watchdog's compute_v3_400_recent check.
--
-- Problem: the previous filter alerted critical on ANY 4xx in
-- net._http_response, including expected client-side validation rejections
-- emitted by the compute_v3 multiplex itself (`{"error":"unknown action ..."}`,
-- `{"error":"missing required field: action"}`, etc.). On 2026-05-23 an
-- interactive probe that called _conan_modal_post_enqueue('compute_v3',
-- {action:'asset_linker_run'}) — an action deliberately excluded from the
-- multiplex (see orchestrator_app.py:655) — tripped a critical flag for a
-- 400 response that was, in fact, the endpoint doing its job.
--
-- Fix: alert only on genuine endpoint-health failures:
--   * 5xx              — server error inside compute_v3 dispatch
--   * timed_out        — pg_net never got a response
--   * error_msg NOT NULL — transport/DNS failure
--   * 4xx other than 400-with-known-validation-body, e.g. 401 (auth fail),
--     403/404/422 (something genuinely wrong with the request/route)
--
-- 400s that carry a known multiplex-validation body are excluded — those mean
-- "the endpoint correctly rejected a malformed/invalid request", which is
-- healthy behavior, not an outage. The validation-body fragments tracked
-- here mirror the strings produced by _dispatch_compute_v3_action and
-- compute_v3_dispatch in modal_workers/orchestrator_app.py; if those strings
-- change there, update this filter accordingly.
--
-- 401 ('invalid or missing x-conan-compute-secret') is intentionally still
-- alerted: persistent 401 means the deployed secret rotated and a caller
-- still has the old one — that IS a real outage of the compute path.
--
-- Only the compute_v3_400_recent block changes. All other checks are byte-for-
-- byte identical to migration 20260601000040.

CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_results jsonb := '{}'::jsonb;
  v_n integer;
  v_current_n integer;
  v_sample jsonb;
  v_cost numeric;
BEGIN
  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'run_id', id,
           'asset_id', asset_id,
           'scheduled_at', scheduled_at,
           'age_seconds', extract(epoch from (now() - scheduled_at))::int
         )) FILTER (WHERE rn <= 5), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, asset_id, scheduled_at,
             row_number() OVER (ORDER BY scheduled_at) AS rn
        FROM public.orchestrator_runs
       WHERE status = 'pending'
         AND tier = 1
         AND scheduled_at < now() - interval '15 minutes'
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'critical',
      'v3_pipeline_watchdog',
      'drainer_tier1_pending_too_long',
      'Tier-1 orchestrator drainer not consuming queue',
      v_n || ' tier=1 row(s) pending >15min. Check cron.job_run_details + net._http_response.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_minutes', 15)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no tier=1 pending rows older than 15 minutes',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'drainer_tier1_pending_too_long'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('drainer_tier1_pending_too_long', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'run_id', id,
           'asset_id', asset_id,
           'scheduled_at', scheduled_at
         )) FILTER (WHERE rn <= 5), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, asset_id, scheduled_at,
             row_number() OVER (ORDER BY scheduled_at) AS rn
        FROM public.orchestrator_runs
       WHERE status = 'pending'
         AND tier = 2
         AND scheduled_at < now() - interval '6 hours'
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'drainer_tier2_pending_too_long',
      'Tier-2 Cowork queue not draining',
      v_n || ' tier=2 row(s) pending > 6h.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_hours', 6)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no tier=2 pending rows older than 6 hours',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'drainer_tier2_pending_too_long'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('drainer_tier2_pending_too_long', v_n);

  SELECT count(*) INTO v_n
    FROM public.v_asset_linker_skill_queue;

  IF v_n > 500 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_backlog',
      'Local asset-linker edge queue is growing',
      v_n || ' prefiltered (document, asset) edges are pending local skill classification.',
      jsonb_build_object('count', v_n, 'threshold', 500, 'mode', 'cursor_skill_edge_queue')
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: local asset-linker queue below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_backlog'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('asset_linker_skill_queue_backlog', v_n);

  SELECT count(*) FILTER (WHERE published_at > now() + interval '30 days'),
         count(*) FILTER (WHERE published_at IS NULL OR published_at <= now() + interval '30 days'),
         COALESCE(jsonb_agg(jsonb_build_object(
           'candidate_id', candidate_id,
           'ticker', ticker,
           'drug_name', drug_name,
           'published_at', published_at,
           'match_strength', match_strength
         ) ORDER BY published_at DESC NULLS LAST) FILTER (WHERE published_at > now() + interval '30 days'), '[]'::jsonb)
    INTO v_n, v_current_n, v_sample
    FROM public.v_asset_linker_skill_queue;

  IF v_n > 50 AND v_current_n = 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_future_dated',
      'Local asset-linker queue is dominated by future-dated edges',
      v_n || ' pending edges have published_at more than 30 days in the future and no current edges are available.',
      jsonb_build_object('count', v_n, 'current_or_null_count', v_current_n, 'threshold', 50, 'sample', v_sample)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: current asset-linker work exists or future-dated edge count is below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_future_dated'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object(
    'asset_linker_skill_queue_future_dated', v_n,
    'asset_linker_skill_queue_current_or_null', v_current_n
  );

  SELECT count(*) INTO v_n
    FROM public.fda_asset_aliases
   WHERE active = true;

  IF v_n = 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_asset_aliases_empty',
      'Supplemental FDA asset alias table is empty',
      'fda_asset_aliases has zero active rows. Layer-1 asset fields still keep the deterministic prefilter functional, but brand/NCT/code recall is missing until seed_fda_asset_aliases runs.',
      jsonb_build_object(
        'active_aliases', 0,
        'next_step', 'run modal_workers.scripts.seed_fda_asset_aliases initial seed'
      )
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: active supplemental aliases exist',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_asset_aliases_empty'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_asset_aliases_active', v_n);

  SELECT count(*)
    INTO v_n
  FROM public.v_asset_linker_skill_queue q
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(q.matched_aliases) AS hit
    WHERE hit->>'kind' <> 'ticker'
  );

  IF v_n > 0
     AND v_n = (SELECT count(*) FROM public.v_asset_linker_skill_queue) THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_ticker_only',
      'Local asset-linker queue is ticker-only',
      'Every pending asset-linker edge is ticker-only. This usually means Layer-1 alias lookup or supplemental alias seeding is not active, so the queue is missing drug/name/code/NCT recall.',
      jsonb_build_object('ticker_only_edges', v_n)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: queue contains non-ticker alias kinds or is empty',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_ticker_only'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('asset_linker_skill_queue_ticker_only', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'asset_id', id,
           'ticker', ticker,
           'drug_name', drug_name,
           'sponsor_name', sponsor_name
         )) FILTER (WHERE rn <= 10), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, ticker, drug_name, sponsor_name,
             row_number() OVER (ORDER BY ticker, created_at DESC) AS rn
        FROM public.fda_assets
       WHERE is_active = true
         AND lower(trim(coalesce(drug_name, ''))) IN (
           '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
         )
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_assets_garbage_program_names',
      'Active fda_assets contain placeholder program names',
      v_n || ' active FDA asset rows use placeholder/garbage drug_name values that can block exact signal-to-asset matching.',
      jsonb_build_object('count', v_n, 'sample', v_sample)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no active placeholder program names found',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_assets_garbage_program_names'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_assets_garbage_program_names', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'asset_id', id,
           'ticker', ticker,
           'drug_name', drug_name,
           'watch_priority', watch_priority,
           'age_days', extract(day from (now() - created_at))::int
         )) FILTER (WHERE rn <= 10), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT a.id, a.ticker, a.drug_name, a.watch_priority, a.created_at,
             row_number() OVER (
               ORDER BY a.watch_priority ASC NULLS LAST, a.created_at ASC
             ) AS rn
        FROM public.fda_assets a
       WHERE a.is_active = true
         AND lower(trim(coalesce(a.drug_name, ''))) NOT IN (
           '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
         )
         AND a.created_at < now() - interval '3 days'
         AND NOT EXISTS (
           SELECT 1 FROM public.asset_documents ad WHERE ad.asset_id = a.id
         )
    ) s;

  IF v_n > 5 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_assets_no_docs',
      'Active fda_assets have no linked documents',
      v_n || ' non-placeholder active assets >3 days old have zero asset_documents.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold', 5)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: active non-placeholder assets without docs below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_assets_no_docs'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_assets_no_docs', v_n);

  -- compute_v3_400_recent — narrowed to genuine endpoint failures.
  -- See file header for rationale and the list of intentionally-excluded
  -- validation bodies.
  SELECT count(*),
         COALESCE(jsonb_agg(DISTINCT substring(content FROM 1 FOR 300)), '[]'::jsonb)
    INTO v_n, v_sample
    FROM net._http_response
   WHERE created > now() - interval '30 minutes'
     AND (
       status_code >= 500
       OR timed_out
       OR error_msg IS NOT NULL
       OR (
         status_code BETWEEN 400 AND 499
         AND NOT (
           status_code = 400
           AND (
             content LIKE '%"error":"unknown action%'
             OR content LIKE '%"error":"missing required field%'
             OR content LIKE '%"error":"payload must be a JSON object%'
             OR content LIKE '%"error":"args must be a JSON object%'
           )
         )
       )
     );

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'critical',
      'v3_pipeline_watchdog',
      'compute_v3_400_recent',
      'compute_v3 endpoint rejecting requests',
      v_n || ' failed compute_v3 HTTP response(s) in last 30 minutes (excluding expected validation rejections).',
      jsonb_build_object('count', v_n, 'sample_responses', v_sample, 'window_minutes', 30)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no compute_v3 HTTP rejects in last 30 minutes (excluding expected validation rejections)',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'compute_v3_400_recent'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('compute_v3_400_recent', v_n);

  UPDATE public.operator_flags
     SET resolved_at = now(),
         resolved_note = 'auto-resolved by _v3_pipeline_watchdog: retired by skill asset-linker/fact-extractor cutover',
         updated_at = now()
   WHERE source IN ('v3_pipeline_watchdog', 'orchestrator_cost')
     AND kind IN (
       'asset_linker_pass1_backlog',
       'asset_linker_pass2_backlog',
       'fact_extractor_stalled',
       'asset_linker_burn_no_output',
       'asset_linker_burn_rate_high',
       'asset_linker_24h_hard_halt'
     )
     AND resolved_at IS NULL;

  SELECT COALESCE(sum(cost_usd), 0) INTO v_cost
    FROM public.asset_linker_runs
   WHERE status IN ('completed', 'budget_exceeded')
     AND completed_at > now() - interval '1 hour';
  v_results := v_results || jsonb_build_object(
    'retired_asset_linker_burn_rate_1h_usd',
    round(v_cost, 4)
  );

  RETURN v_results;
END;
$function$;

COMMENT ON FUNCTION public._v3_pipeline_watchdog() IS
  'Runs v3 pipeline health checks and writes operator_flags. compute_v3 health '
  'check excludes expected multiplex validation rejections (unknown action, '
  'missing field, malformed payload/args) — those are healthy behavior, not '
  'outages. 401 (bad secret), 5xx, timed_out, and transport errors still alert.';
