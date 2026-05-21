-- Operator flags cleanup and severity-contract fixes.
--
-- Runtime evidence from 2026-05-21 showed:
--   * operator_flags.severity only permits info/warn/critical, while app code
--     had started emitting severity='error';
--   * the live orphan sweeper skipped tier-2 orphan convergence_assessments;
--   * several operator_flags were stale after their underlying condition cleared;
--   * future-dated asset-linker edges were intentionally retained for recall but
--     the watchdog still treated them as a warning even when current work exists.

-- Tier-2 bulk/Cowork rows can orphan just like tier-1 rows. If valid rows point
-- at an orphan as their superseded_by target, first rewire them to the next
-- valid newer assessment for the same asset (or NULL when none exists), then
-- delete the zero-metric rows.
CREATE OR REPLACE FUNCTION public.cleanup_orphaned_assessments()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_deleted_ids uuid[];
  v_count int;
BEGIN
  DROP TABLE IF EXISTS pg_temp.tmp_orphan_assessments_for_cleanup;

  CREATE TEMP TABLE tmp_orphan_assessments_for_cleanup (
    id uuid PRIMARY KEY,
    asset_id uuid NOT NULL,
    created_at timestamptz NOT NULL
  ) ON COMMIT DROP;

  INSERT INTO tmp_orphan_assessments_for_cleanup (id, asset_id, created_at)
  SELECT ca.id, ca.asset_id, ca.created_at
    FROM public.convergence_assessments ca
   WHERE ca.created_at < now() - interval '15 minutes'
     AND COALESCE(ca.tier, 1) IN (1, 2)
     AND NOT EXISTS (
       SELECT 1
       FROM public.assessment_stage_metrics asm
       WHERE asm.assessment_id = ca.id
     );

  UPDATE public.convergence_assessments ref
     SET superseded_by = (
       SELECT ca2.id
         FROM public.convergence_assessments ca2
        WHERE ca2.asset_id = ref.asset_id
          AND ca2.created_at > ref.created_at
          AND NOT EXISTS (
            SELECT 1
              FROM tmp_orphan_assessments_for_cleanup o2
             WHERE o2.id = ca2.id
          )
          AND EXISTS (
            SELECT 1
              FROM public.assessment_stage_metrics asm2
             WHERE asm2.assessment_id = ca2.id
          )
        ORDER BY ca2.created_at ASC
        LIMIT 1
     )
    FROM tmp_orphan_assessments_for_cleanup orphan
   WHERE ref.superseded_by = orphan.id
     AND NOT EXISTS (
       SELECT 1
         FROM tmp_orphan_assessments_for_cleanup self_orphan
        WHERE self_orphan.id = ref.id
     );

  UPDATE public.convergence_assessments orphan_row
     SET superseded_by = NULL
    FROM tmp_orphan_assessments_for_cleanup orphan
   WHERE orphan_row.id = orphan.id
     AND orphan_row.superseded_by IS NOT NULL;

  WITH orphans AS (
    DELETE FROM public.convergence_assessments ca
    USING tmp_orphan_assessments_for_cleanup orphan
    WHERE ca.id = orphan.id
    RETURNING ca.id
  )
  SELECT array_agg(id) FROM orphans INTO v_deleted_ids;

  v_count := COALESCE(array_length(v_deleted_ids, 1), 0);

  IF v_count > 0 THEN
    INSERT INTO public.operator_flags (
      severity, source, kind, title, body, evidence
    ) VALUES (
      'warn',
      'orphan_sweeper',
      'convergence_orphan_deleted',
      format('Cleaned up %s orphan convergence_assessments row(s)', v_count),
      'Orphan parent rows: tier in (1,2), zero assessment_stage_metrics children, '
        || 'older than 15 minutes. Valid rows that pointed at these orphan '
        || 'supersede targets were rewired before deletion.',
      jsonb_build_object(
        'deleted_ids', to_jsonb(v_deleted_ids),
        'deleted_count', v_count,
        'sweeper_run_at', to_jsonb(now())
      )
    )
    ON CONFLICT DO NOTHING;
  END IF;

  RETURN v_count;
END;
$$;

COMMENT ON FUNCTION public.cleanup_orphaned_assessments() IS
  'Deletes tier-1/tier-2 convergence_assessments rows older than 15 minutes '
  'with zero assessment_stage_metrics children after rewiring valid '
  'superseded_by references to the next valid newer assessment. Emits an '
  'orphan_sweeper operator_flag when rows are removed.';

-- Resolve stale flags whose live predicates are no longer true.
UPDATE public.operator_flags f
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: candidate is no longer active',
       updated_at = now()
  FROM public.candidates c
 WHERE f.resolved_at IS NULL
   AND f.source = 'reporting_weekly'
   AND f.kind = 'stuck_active_candidate'
   AND f.candidate_id = c.id
   AND c.state::text <> 'active';

UPDATE public.operator_flags f
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: catalyst_universe now has material rows in the audited window',
       updated_at = now()
 WHERE f.resolved_at IS NULL
   AND f.source = 'reporting_weekly'
   AND f.kind = 'coverage_empty_window'
   AND EXISTS (
     SELECT 1
       FROM public.catalyst_universe cu
      WHERE cu.catalyst_date BETWEEN (f.evidence->>'window_start')::date
                                 AND (f.evidence->>'window_end')::date
        AND cu.material_outcome = 'yes'
   );

UPDATE public.operator_flags f
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: newer tier2_quality PDUFA radar summary supersedes this manual summary',
       updated_at = now()
 WHERE f.resolved_at IS NULL
   AND f.source = 'manual'
   AND f.kind = 'pdufa_radar_summary'
   AND EXISTS (
     SELECT 1
       FROM public.operator_flags newer
      WHERE newer.resolved_at IS NULL
        AND newer.source = 'tier2_quality'
        AND newer.kind = 'pdufa_radar_summary'
        AND newer.created_at > f.created_at
   );

UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: fact_extractor_opus automated path was retired by the local skill asset-linker cutover',
       updated_at = now()
 WHERE resolved_at IS NULL
   AND source = 'skill_watchdog'
   AND kind = 'skill_dark:fact_extractor_opus';

-- Dispatched historical alerts are retained as audit history. The open flag is
-- noise once all orphaned alerts have already been dispatched and no thesis_job
-- references them.
UPDATE public.operator_flags f
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: historical dispatched orphan alerts retained as audit records',
       updated_at = now()
 WHERE f.resolved_at IS NULL
   AND f.source IN ('reporting_weekly', 'convergence_qa')
   AND f.kind = 'orphan_alert'
   AND NOT EXISTS (
     SELECT 1
       FROM public.alerts a
       JOIN public.signals s ON s.signal_id = a.signal_id
       LEFT JOIN public.thesis_jobs tj ON tj.alert_id = a.id
      WHERE COALESCE(s.band_with_bonus, s.band)::text <> 'immediate'
        AND (a.dispatched_at IS NULL OR tj.id IS NOT NULL)
   );

UPDATE public.operator_flags f
   SET resolved_at = now(),
       resolved_note = 'auto-resolved: orphan sweeper has no remaining tier-1/tier-2 zero-metric assessment rows',
       updated_at = now()
 WHERE f.resolved_at IS NULL
   AND f.source = 'orphan_sweeper'
   AND f.kind = 'convergence_orphan_deleted'
   AND NOT EXISTS (
     SELECT 1
       FROM public.convergence_assessments ca
      WHERE ca.created_at < now() - interval '15 minutes'
        AND COALESCE(ca.tier, 1) IN (1, 2)
        AND NOT EXISTS (
          SELECT 1
            FROM public.assessment_stage_metrics asm
           WHERE asm.assessment_id = ca.id
        )
   );

-- Future-dated edges are expected after the recall fix: the view orders current
-- and high-signal edges first. Warn only if the queue has no current work and is
-- dominated by far-future edges.
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

  SELECT count(*),
         COALESCE(jsonb_agg(DISTINCT substring(content FROM 1 FOR 300)), '[]'::jsonb)
    INTO v_n, v_sample
    FROM net._http_response
   WHERE created > now() - interval '30 minutes'
     AND (
       status_code >= 400
       OR timed_out
       OR error_msg IS NOT NULL
     );

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'critical',
      'v3_pipeline_watchdog',
      'compute_v3_400_recent',
      'compute_v3 endpoint rejecting requests',
      v_n || ' failed compute_v3 HTTP response(s) in last 30 minutes.',
      jsonb_build_object('count', v_n, 'sample_responses', v_sample, 'window_minutes', 30)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no compute_v3 HTTP rejects in last 30 minutes',
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
  'Runs v3 pipeline health checks and writes operator_flags. Future-dated '
  'asset-linker edges warn only when no current/null-dated queue work exists.';
