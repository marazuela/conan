-- =============================================================================
-- fda_application_submissions — structured per-submission record from openFDA
-- drug/drugsfda, the keystone feed for the empirical CRL rubrics.
--
-- The drugsfda ingest (modal_workers/ingestion/openfda_ingest.py) already
-- fetches the full `submissions[]` array per application but only renders it
-- into document raw_text. This table persists it structured so the CRL
-- feature-assembly (modal_workers/shared/fda_crl/feature_assembly.py, Phase 2)
-- can read it without re-parsing prose.
--
-- It is the single source for THREE things at once:
--   * NDA  type5_or_3       <- submission_class_code TYPE 3 / TYPE 5
--   * NDA/sNDA priority      <- review_priority = PRIORITY
--   * router original/suppl  <- submission_type ORIG vs SUPPL
--   * sNDA act_* flags       <- submission_class_code (NEW INDICATION / DOSING / ...)
--
-- Key shape: (application_number, submission_type, submission_number) — openFDA's
-- natural submission identity. Upserted (resolution=merge-duplicates) so the
-- daily ingest refreshes status/class on resubmission without fanning rows.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.fda_application_submissions (
  application_number                text NOT NULL,
  submission_type                   text NOT NULL,   -- ORIG | SUPPL
  submission_number                 text NOT NULL,   -- submission id (S-number etc.)
  submission_status                 text,            -- AP | TA | CR | WD | RL | ...
  submission_class_code             text,            -- TYPE 1..10 | EFFICACY | LABELING | MANUF (CMC) | BIOSIMILAR | ...
  submission_class_code_description text,
  review_priority                   text,            -- PRIORITY | STANDARD | NULL
  submission_status_date            date,
  sponsor_name                      text,
  ticker                            text,            -- best-effort resolve_sponsor() result at ingest
  source                            text NOT NULL DEFAULT 'openfda_drugsfda',
  refreshed_at                      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (application_number, submission_type, submission_number)
);

CREATE INDEX IF NOT EXISTS fda_application_submissions_sponsor_idx
  ON public.fda_application_submissions (sponsor_name);
CREATE INDEX IF NOT EXISTS fda_application_submissions_appno_idx
  ON public.fda_application_submissions (application_number);
CREATE INDEX IF NOT EXISTS fda_application_submissions_ticker_idx
  ON public.fda_application_submissions (ticker)
  WHERE ticker IS NOT NULL;

COMMENT ON TABLE public.fda_application_submissions IS
  'Structured openFDA drugsfda submissions. Keystone feed for the empirical CRL rubrics (modal_workers/shared/fda_crl): drives NDA type5_or_3 + priority, the original-vs-supplement router, and sNDA supplement-type flags. Populated by openfda_ingest._upsert_application_submissions.';
