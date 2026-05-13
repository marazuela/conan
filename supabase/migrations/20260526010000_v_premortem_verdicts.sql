-- 20260526010000_v_premortem_verdicts.sql
-- Wave 6.2 — pre-mortem verdict telemetry views.
--
-- The all_falsified ceiling (Stage 9 post-hoc cap) hard-caps conviction at
-- ORCH_ALL_FALSIFIED_CEILING (default 30) whenever Stage 3 returns the
-- 'all_falsified' overall_verdict. Today we have no easy view onto how
-- often that cap fires versus the 'partial' / 'all_survive' verdicts.
--
-- These views give the operator a 7-day and 30-day rolling window over
-- pre_mortem_verdict distributions, so when the cap is over- or under-firing
-- we notice. Direct table queries work too — these views just save typing.

CREATE OR REPLACE VIEW public.v_premortem_verdicts_7d AS
SELECT
  pre_mortem_verdict,
  COUNT(*)                                    AS n,
  ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS pct,
  ROUND(AVG(conviction_pct)::numeric, 1)      AS avg_conviction,
  ROUND(AVG(conviction_pct_calibrated)::numeric, 1) AS avg_conviction_calibrated,
  ROUND(AVG(raw_conviction_pct)::numeric, 1)  AS avg_raw_conviction
FROM public.convergence_assessments
WHERE created_at > NOW() - INTERVAL '7 days'
  AND pre_mortem_verdict IS NOT NULL
GROUP BY pre_mortem_verdict
ORDER BY n DESC;

CREATE OR REPLACE VIEW public.v_premortem_verdicts_30d AS
SELECT
  pre_mortem_verdict,
  COUNT(*)                                    AS n,
  ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS pct,
  ROUND(AVG(conviction_pct)::numeric, 1)      AS avg_conviction,
  ROUND(AVG(conviction_pct_calibrated)::numeric, 1) AS avg_conviction_calibrated,
  ROUND(AVG(raw_conviction_pct)::numeric, 1)  AS avg_raw_conviction
FROM public.convergence_assessments
WHERE created_at > NOW() - INTERVAL '30 days'
  AND pre_mortem_verdict IS NOT NULL
GROUP BY pre_mortem_verdict
ORDER BY n DESC;

COMMENT ON VIEW public.v_premortem_verdicts_7d IS
  'Wave 6.2 — distribution of Stage 3 pre_mortem_verdict over last 7d. '
  'Use to monitor how often the all_falsified ceiling (Stage 9 cap) fires.';
COMMENT ON VIEW public.v_premortem_verdicts_30d IS
  'Wave 6.2 — 30-day variant of v_premortem_verdicts_7d.';
