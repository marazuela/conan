-- =============================================================================
-- fda_drug_inspections — FDA drug/biologic facility inspection classifications,
-- the source for the NDA rubric's `n_drug_inspections_5y` feature.
--
-- Source: FDA Inspections Classification dataset (ORA / FDA Data Dashboard).
-- Populated by modal_workers/fetchers/universe/fda_inspections.py. Firm legal
-- names are matched to drug sponsors best-effort (resolve_sponsor) at ingest;
-- the feature-assembly counts inspections in a trailing-5y window per sponsor.
--
-- Sponsor↔firm matching is fuzzy (inspection firm legal names differ from
-- drugsfda sponsor_name), so `sponsor_ticker` is best-effort and `firm_name_norm`
-- (LOWER(TRIM(collapse ws))) is kept for name-based fallback joins in assembly.
--
-- Key: inspection_id — deterministic hash of (fei_or_firm_norm, end_date,
-- classification, product_type) so re-ingests are idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.fda_drug_inspections (
  inspection_id        text NOT NULL,
  fei_number           text,
  firm_name            text NOT NULL,
  firm_name_norm       text NOT NULL,   -- LOWER(TRIM(collapse interior ws))
  inspection_end_date  date,
  classification       text,            -- NAI | VAI | OAI
  product_type         text,            -- Drugs | Biologics | ...
  posted_citations     boolean,
  sponsor_ticker       text,            -- best-effort resolve_sponsor() at ingest
  source               text NOT NULL DEFAULT 'fda_inspections_classification',
  refreshed_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (inspection_id)
);

CREATE INDEX IF NOT EXISTS fda_drug_inspections_firm_norm_idx
  ON public.fda_drug_inspections (firm_name_norm);
CREATE INDEX IF NOT EXISTS fda_drug_inspections_ticker_idx
  ON public.fda_drug_inspections (sponsor_ticker)
  WHERE sponsor_ticker IS NOT NULL;
CREATE INDEX IF NOT EXISTS fda_drug_inspections_end_date_idx
  ON public.fda_drug_inspections (inspection_end_date);

COMMENT ON TABLE public.fda_drug_inspections IS
  'FDA drug/biologic facility inspection classifications (NAI/VAI/OAI). Source for the NDA CRL rubric n_drug_inspections_5y feature. Populated by fetchers/universe/fda_inspections.py; firm↔sponsor match is best-effort.';
