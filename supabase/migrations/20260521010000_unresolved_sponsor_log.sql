-- 2026-05-11 — unresolved_sponsor_log (R4 Phase 1 telemetry)
--
-- Captures every (sponsor_name, scanner_run_id) pair where a scanner cleared
-- the upstream gates but could not resolve the sponsor to a public issuer
-- (sec_issuer_lookup miss, no FIGI). The downstream signal still emits, just
-- with name-only entity hints — over time these accumulate as orphan entities
-- in the entities table.
--
-- The pre_phase3 today writes ~57 unresolved sponsors per run (mostly EU/Asia
-- listings + private biotechs). This table makes the distribution queryable:
--
--   SELECT sponsor_name_normalized, COUNT(*) AS misses,
--          MAX(observed_at) AS last_seen
--     FROM public.unresolved_sponsor_log
--    WHERE observed_at > NOW() - INTERVAL '14 days'
--    GROUP BY sponsor_name_normalized
--    ORDER BY misses DESC
--    LIMIT 50;
--
-- After 1-2 weeks of data, Phase 2A (seed-migration extension) operates on
-- top-frequency misses; Phase 2B (OpenFIGI name-search fallback) is justified
-- only if the long tail dominates.

CREATE TABLE IF NOT EXISTS public.unresolved_sponsor_log (
  id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sponsor_name             text        NOT NULL,
  -- Normalized form for frequency aggregation (lowercase, trimmed,
  -- punctuation-stripped, suffix-stripped). Populated by the writer; we
  -- store it explicitly so SQL aggregation doesn't pay a per-row regex cost.
  sponsor_name_normalized  text        NOT NULL,
  scanner_name             text        NOT NULL,
  scanner_run_id           uuid        REFERENCES public.scanner_runs(id) ON DELETE SET NULL,
  observed_at              timestamptz NOT NULL DEFAULT NOW(),
  -- Per-occurrence context (e.g. {"nct_id": "NCT123", "trial_phase": "PHASE3"}).
  -- Keep small (<1KB); for diagnostic spelunking, not for analytics.
  context                  jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS unresolved_sponsor_log_normalized_observed_idx
  ON public.unresolved_sponsor_log (sponsor_name_normalized, observed_at DESC);

CREATE INDEX IF NOT EXISTS unresolved_sponsor_log_run_idx
  ON public.unresolved_sponsor_log (scanner_run_id)
  WHERE scanner_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS unresolved_sponsor_log_observed_at_idx
  ON public.unresolved_sponsor_log (observed_at DESC);

COMMENT ON TABLE public.unresolved_sponsor_log IS
  'Per-occurrence log of scanner sponsor resolution misses. Populated by '
  'scanners that have INDUSTRY-class gating + name-only entity hints '
  '(pre_phase3_readout_scanner today; binary_catalyst scanners as they adopt). '
  'Frequency rank against this to drive seed-migration aliases (Phase 2A) or '
  'justify an OpenFIGI name-search fallback (Phase 2B).';
