-- fda_assets: designation + sponsor-history columns for the binary-catalyst pre-gate.
--
-- The pre-gate (supabase/functions/reactor/bc-pregate.ts) needs structural-quality
-- inputs (breakthrough designation, priority review, first-time sponsor) at dispatch
-- time. Those were previously sourced from the scanner's per-scan openFDA enrichment,
-- which is budget-starved and populated <2% of signals -> every input read false and
-- the gate scored every asset 0.
--
-- Fix: hydrate the inputs onto the entity (fda_assets) via a dedicated enricher
-- (modal_workers/scripts/enrich_fda_asset_designations.py) on its own budget, and have
-- the gate read them here at dispatch -- exactly as it already reads class_precedent
-- from fda_assets (mechanism, indication) -> fda_class_precedent_base_rates.
--
-- All columns are nullable; NULL designations_enriched_at means "not yet enriched",
-- which the gate treats as enrichment_state='stub' (auto-decline in active mode, re-
-- evaluated once the enricher runs).

ALTER TABLE public.fda_assets
  ADD COLUMN IF NOT EXISTS priority_review boolean,
  ADD COLUMN IF NOT EXISTS breakthrough_designation boolean,
  ADD COLUMN IF NOT EXISTS sponsor_prior_nda_count integer,
  ADD COLUMN IF NOT EXISTS first_time_sponsor boolean,
  ADD COLUMN IF NOT EXISTS designations_enriched_at timestamptz;

COMMENT ON COLUMN public.fda_assets.priority_review IS
  'openFDA drugsfda review_priority=PRIORITY on the in-flight submission. Pre-gate input (+3). Hydrated by enrich_fda_asset_designations.py.';
COMMENT ON COLUMN public.fda_assets.breakthrough_designation IS
  'Breakthrough-therapy designation. Best-effort: openFDA drugsfda does not carry it, so this is true only when a label/8-K surfaces it; usually false. Pre-gate input (+6).';
COMMENT ON COLUMN public.fda_assets.sponsor_prior_nda_count IS
  'Count of the sponsor''s prior distinct NDA/BLA application numbers (openFDA drugsfda, capped at one page). NULL = lookup not yet run. Audit/derivation source for first_time_sponsor.';
COMMENT ON COLUMN public.fda_assets.first_time_sponsor IS
  'sponsor_prior_nda_count = 0 on a confirmed lookup. Pre-gate input (+4). NULL/false on "unknown" so the bonus only fires on a confirmed zero.';
COMMENT ON COLUMN public.fda_assets.designations_enriched_at IS
  'Last time enrich_fda_asset_designations.py wrote the designation/sponsor columns. NULL => gate reads enrichment_state=stub (auto-decline in active mode, re-eval after enrichment).';

-- Enricher work-queue: active assets that are stale or never enriched.
CREATE INDEX IF NOT EXISTS fda_assets_designations_enriched_at_idx
  ON public.fda_assets (designations_enriched_at NULLS FIRST)
  WHERE is_active IS NOT FALSE;
COMMENT ON INDEX public.fda_assets_designations_enriched_at_idx IS
  'Speeds the designation enricher''s "active assets needing (re)enrichment" scan (NULL-first = unenriched go first).';
