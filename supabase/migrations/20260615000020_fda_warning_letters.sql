-- =============================================================================
-- fda_warning_letters — FDA Warning Letters, the source for the NDA rubric's
-- `sponsor_has_warning` feature (sponsor-level warning-letter history).
--
-- Source: FDA Warning Letters dataset (fda.gov / FDA Data Dashboard compliance
-- actions). Populated by modal_workers/fetchers/universe/fda_warning_letters.py.
-- `documents.source='fda_warning_letter'` already captures letters at the
-- document level; this table adds the entity-linked feature view the rubric
-- needs (sponsor → has-prior-warning), which the document corpus cannot answer
-- without a join the feature-assembly should not be re-deriving each call.
--
-- Firm↔sponsor matching is fuzzy; `sponsor_ticker` is best-effort and
-- `firm_name_norm` is kept for name-based fallback joins.
--
-- Key: letter_id — deterministic hash of (firm_name_norm, issue_date, subject)
-- so re-ingests are idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.fda_warning_letters (
  letter_id        text NOT NULL,
  firm_name        text NOT NULL,
  firm_name_norm   text NOT NULL,   -- LOWER(TRIM(collapse interior ws))
  issue_date       date,
  letter_url       text,
  issuing_office   text,
  subject          text,
  sponsor_ticker   text,            -- best-effort resolve_sponsor() at ingest
  source           text NOT NULL DEFAULT 'fda_warning_letters',
  refreshed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (letter_id)
);

CREATE INDEX IF NOT EXISTS fda_warning_letters_firm_norm_idx
  ON public.fda_warning_letters (firm_name_norm);
CREATE INDEX IF NOT EXISTS fda_warning_letters_ticker_idx
  ON public.fda_warning_letters (sponsor_ticker)
  WHERE sponsor_ticker IS NOT NULL;
CREATE INDEX IF NOT EXISTS fda_warning_letters_issue_date_idx
  ON public.fda_warning_letters (issue_date);

COMMENT ON TABLE public.fda_warning_letters IS
  'FDA Warning Letters, entity-linked. Source for the NDA CRL rubric sponsor_has_warning feature. Populated by fetchers/universe/fda_warning_letters.py; firm↔sponsor match is best-effort.';
