-- documents.linker_classified_result: add 'source_not_eligible' to the CHECK list.
--
-- Background: PR #52 added source-routing for asset_linker pass-1
-- (only `clinicaltrials` runs through Sonnet for the current trial-stage
-- universe; gold-set evidence in plans/asset-linker-yield-optimization.md).
-- Docs in non-eligible sources (edgar, openfda, dailymed, federal_register)
-- never reach the classifier, so their linker_classified_at stays NULL forever
-- — which the v3_pipeline_watchdog's pass-1 backlog check counts as backlog.
--
-- This migration adds a fourth result value, 'source_not_eligible', that a
-- one-shot sweep (run separately) can stamp on those rows so they look
-- properly classified to the watchdog. New incoming docs in non-eligible
-- sources still need a separate stamping path (deferred — when the eligibility
-- table redesign lands, that path can mark rows synchronously on ingest).

ALTER TABLE public.documents
  DROP CONSTRAINT IF EXISTS documents_linker_classified_result_check;

ALTER TABLE public.documents
  ADD CONSTRAINT documents_linker_classified_result_check
  CHECK (
    linker_classified_result IS NULL
    OR linker_classified_result = ANY (ARRAY[
      'linked'::text,
      'no_match'::text,
      'parse_error'::text,
      'source_not_eligible'::text
    ])
  );

COMMENT ON COLUMN public.documents.linker_classified_result IS
  'Outcome of asset_linker pass-1: linked | no_match | parse_error | source_not_eligible. '
  'source_not_eligible marks docs that asset_linker.SOURCE_ALLOWLIST excludes — they '
  'never reach the model, but are stamped synchronously so the backlog metric stays clean.';
