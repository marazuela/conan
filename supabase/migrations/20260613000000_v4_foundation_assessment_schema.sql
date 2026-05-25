-- v4 foundation: schema additions for the v4 orchestrator pipeline.
--
-- This migration lands the columns Phase 2/7 of the v4 architecture
-- simplification (~/.claude/plans/proud-booping-seal.md) will write to.
-- All columns are nullable so existing v3-emitted rows remain valid; the
-- v3 codepath does not populate them and is unaffected.
--
-- New columns on convergence_assessments:
--   commercial_dimensions jsonb
--     Holds the commercial half of the v4 Stage 1 output: TAM, mcap_to_tam,
--     standard_of_care, soc_limitations, soc_side_effects,
--     unmet_need_severity_1_5, regulatory_incentive_signals[], etc. Shape
--     is defined by conan-cowork-skills/schemas/commercial_opportunity_v1.json
--     (added in Phase 2) but the column is jsonb to allow schema evolution
--     without further migrations.
--
--   orchestrator_version_v4 boolean
--     Marks rows produced by the v4 codepath. Distinct from the existing
--     `orchestrator_version` text column (e.g. "orch-v0.4.0-mvp") because
--     this is a clean A/B partition key for cost/quality comparison
--     during the Phase 6 flag-flip observation window. Defaults to FALSE so
--     all existing rows + new v3 rows read as "not v4".
--
--   signal_category text
--     The originating signal taxonomy bucket (e.g. 'fda_pdufa',
--     'fda_adcom', 'insider_activity', 'shareholder_structure',
--     'cross_source', 'literature'). Populated by the v4 Stage 10 from the
--     trigger signal's category. Enables Phase 7's per-category accuracy
--     aggregation without re-derivation from upstream rows.
--
-- New columns on post_mortem_queue:
--   signal_category text
--     Copied from the source convergence_assessment at queue time so the
--     feedback loop can group resolved outcomes by category without a
--     join back to convergence_assessments (which may have superseded
--     rows). Nullable; backfilled lazily.

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS commercial_dimensions jsonb;

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS orchestrator_version_v4 boolean
    NOT NULL DEFAULT false;

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS signal_category text;

ALTER TABLE public.post_mortem_queue
  ADD COLUMN IF NOT EXISTS signal_category text;

-- Partial index for Phase 6/7: per-category accuracy aggregation reads
-- the v4-only resolved cohort. WHERE clause keeps the index small while
-- v3 rows dominate; expands automatically as v4 rolls out.
CREATE INDEX IF NOT EXISTS idx_convergence_assessments_v4_signal_category
  ON public.convergence_assessments (signal_category, created_at DESC)
  WHERE orchestrator_version_v4 = true AND signal_category IS NOT NULL;

COMMENT ON COLUMN public.convergence_assessments.commercial_dimensions IS
  'v4 Stage 1 commercial-opportunity output: TAM, mcap_to_tam, SoC, '
  'unmet_need_severity, regulatory_incentive_signals. NULL on v3 rows.';

COMMENT ON COLUMN public.convergence_assessments.orchestrator_version_v4 IS
  'TRUE if produced by the v4 single-pass orchestrator (runtime_v4.py); '
  'FALSE for legacy v3 rows. Used as A/B partition key during Phase 6 '
  'flag-flip observation.';

COMMENT ON COLUMN public.convergence_assessments.signal_category IS
  'Originating signal taxonomy bucket. Stamped by Stage 10 from the '
  'trigger signal. Phase 7 feedback loop groups accuracy by this column.';

COMMENT ON COLUMN public.post_mortem_queue.signal_category IS
  'Copied from convergence_assessments.signal_category at queue time so '
  'post-resolution per-category metrics survive supersedence of the '
  'source assessment.';
