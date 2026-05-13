-- asset_linker_gold_set: human-arbitrable ground truth for asset_linker pass-1 evaluation.
--
-- Purpose: enable measurement-based tuning of the asset_linker prefilter and model
-- choice. Without ground truth we can't tell whether a low link rate means "the
-- prefilter is leaking noise" (fixable) or "the asset universe doesn't match the
-- corpus" (corpus-floor, unfixable at the linker layer).
--
-- Owner: investigation in ~/.claude/plans/asset-linker-yield-optimization.md.
-- Phase 1 populates this table with ~500 stratified-by-source docs labeled by
-- Opus 4.7 (with Sonnet 4.7 as a second labeler on disagreements). Phase 2 replays
-- the gold set through the current prefilter / Haiku pass-1 / Sonnet pass-1 to
-- isolate the failure mode. Phase 4 uses the same table for continuous regression
-- monitoring (daily Opus re-labels 1-2% of new docs; recall drift opens an
-- operator_flag).

CREATE TABLE IF NOT EXISTS public.asset_linker_gold_set (
  doc_id              uuid PRIMARY KEY REFERENCES public.documents(id) ON DELETE CASCADE,
  -- empty array = "this doc mentions no tracked asset" (negative label).
  -- non-empty = list of fda_assets.id values the doc materially discusses.
  true_asset_ids      uuid[] NOT NULL DEFAULT '{}',
  -- 'high' = both labelers agreed (or single high-confidence labeler).
  -- 'low'  = single labeler / unresolved disagreement / ambiguous span.
  confidence          text   NOT NULL CHECK (confidence IN ('high', 'low')),
  -- Span evidence: for each asset_id, the text snippet that justifies the link.
  -- Shape: [{asset_id, span, link_type}, ...] where link_type matches the
  -- asset_linker.py LINK_TYPES set: primary | mentions | pipeline_context |
  -- safety_signal | literature.
  spans               jsonb  NOT NULL DEFAULT '[]'::jsonb,
  -- Which model(s) produced this label. Multi-labeler = agreement.
  labeler_models      text[] NOT NULL,
  labeled_at          timestamptz NOT NULL DEFAULT now(),
  -- Stratification source for the sample (matches documents.source).
  -- Lets us slice precision/recall per source feed.
  source_at_sample    text,
  -- Free-form notes from labeler (e.g. "ambiguous — drug name matches but
  -- discusses competitor's trial"). Useful for refining the prefilter.
  notes               text
);

-- Indexes for the typical eval queries:
--   - "what fraction of gold-set docs from source X have non-empty true_asset_ids"
--   - "join against asset_documents to compute pass-1 confusion matrix"
CREATE INDEX IF NOT EXISTS asset_linker_gold_set_source_idx
  ON public.asset_linker_gold_set (source_at_sample);

CREATE INDEX IF NOT EXISTS asset_linker_gold_set_nonempty_idx
  ON public.asset_linker_gold_set ((array_length(true_asset_ids, 1)))
  WHERE array_length(true_asset_ids, 1) > 0;

-- RLS: dashboard reads via authenticated role. Follows the pattern from
-- 20260522000000_rls_select_policies_for_dashboard_tables.sql.
ALTER TABLE public.asset_linker_gold_set ENABLE ROW LEVEL SECURITY;

CREATE POLICY asset_linker_gold_set_select_all
  ON public.asset_linker_gold_set
  FOR SELECT
  TO authenticated
  USING (true);

COMMENT ON TABLE  public.asset_linker_gold_set IS
  'Human-arbitrable ground truth for asset_linker pass-1 evaluation. Populated by '
  '~/.claude/plans/asset-linker-yield-optimization.md Phase 1 — Opus 4.7 + Sonnet 4.7 '
  'two-pass labeling over a stratified-by-source sample.';
COMMENT ON COLUMN public.asset_linker_gold_set.true_asset_ids IS
  'Empty array = negative label (no tracked asset mentioned). Non-empty = list of '
  'fda_assets.id values the doc materially discusses.';
COMMENT ON COLUMN public.asset_linker_gold_set.confidence IS
  'high: both labelers agreed. low: single labeler / unresolved disagreement.';
