-- =============================================================================
-- fda_class_precedent_base_rates — pre-computed class-peer base rates for
-- the BC convergence pre-gate.
--
-- Replaces the v1 `class_precedent = 0` stub in
-- `supabase/functions/reactor/bc-pregate.ts`. The reactor reads from this
-- table by (moa_canonical, indication) at gate time and feeds the
-- approval_rate into the BC scoring formula. With this term active, the
-- gate's composite max climbs from 10 to 15 (BT+6 + sponsor+4 + class+5),
-- and the operator should bump `internal_config.bc_pregate_threshold`
-- from 6 to 9 after the refresher has seeded a representative population.
--
-- Key shape: (moa_canonical, indication). Pedro 2026-05-25 — chosen so the
-- reactor can join either dimension via a partial index when one side is
-- unknown, and so newer mechanisms don't silently inherit older mechanisms'
-- base rates.
--
-- v1 normalization: `moa_canonical` is `LOWER(TRIM(fda_assets.mechanism))`,
-- `indication` is `LOWER(TRIM(fda_assets.indication))`. v2 (post-ChEMBL
-- integration) will store the canonical-form mechanism — same schema,
-- sharper values. The `source` column documents which refresh path filled
-- the row so we can re-emit selectively if v2 normalization lands.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.fda_class_precedent_base_rates (
  moa_canonical    text NOT NULL,
  indication       text NOT NULL,
  n_approvals      int  NOT NULL DEFAULT 0 CHECK (n_approvals >= 0),
  n_crls           int  NOT NULL DEFAULT 0 CHECK (n_crls >= 0),
  approval_rate    numeric(5,4) CHECK (approval_rate IS NULL OR (approval_rate >= 0 AND approval_rate <= 1)),
  ci_low           numeric(5,4) CHECK (ci_low IS NULL OR (ci_low >= 0 AND ci_low <= 1)),
  ci_high          numeric(5,4) CHECK (ci_high IS NULL OR (ci_high >= 0 AND ci_high <= 1)),
  lookback_years   int  NOT NULL DEFAULT 10 CHECK (lookback_years > 0),
  refreshed_at     timestamptz NOT NULL DEFAULT now(),
  source           text NOT NULL DEFAULT 'fda_regulatory_events'
    CHECK (source IN ('fda_regulatory_events','openfda','hybrid','manual')),
  PRIMARY KEY (moa_canonical, indication)
);

CREATE INDEX IF NOT EXISTS fda_class_precedent_base_rates_refreshed_idx
  ON public.fda_class_precedent_base_rates (refreshed_at DESC);

COMMENT ON TABLE public.fda_class_precedent_base_rates IS
  'Pre-computed (moa_canonical, indication) approval base rates. Refreshed '
  'nightly by bc_class_precedent_refresher.py from fda_regulatory_events. '
  'Read by reactor bc-pregate at gate time to fill the class_precedent input '
  'in the BC convergence pre-gate score (D-129 WI-2 follow-up).';

COMMENT ON COLUMN public.fda_class_precedent_base_rates.moa_canonical IS
  'Lowercased + trimmed mechanism-of-action. v1: raw mechanism string from '
  'fda_assets.mechanism. v2 (post-ChEMBL): canonical-form MoA.';

COMMENT ON COLUMN public.fda_class_precedent_base_rates.approval_rate IS
  'n_approvals / (n_approvals + n_crls). NULL when no decisions seen in the '
  'lookback window — reactor treats NULL as class_precedent=0.';

COMMENT ON COLUMN public.fda_class_precedent_base_rates.ci_low IS
  'Wilson interval lower bound on approval_rate. NULL when n_total = 0.';

-- Refresher service-role can upsert; everyone else reads.
ALTER TABLE public.fda_class_precedent_base_rates ENABLE ROW LEVEL SECURITY;

CREATE POLICY fda_class_precedent_base_rates_select
  ON public.fda_class_precedent_base_rates
  FOR SELECT TO authenticated USING (true);

-- internal_config row so the operator can verify the refresher pipeline is
-- pointed at a Modal endpoint before the nightly pg_cron starts firing. Empty
-- value keeps the cron job a no-op (matches the Phase 3a pattern).
-- internal_config has no description column on this project; intent documented
-- in the cron migration's COMMENT instead. Bump bc_pregate_threshold from 6 to
-- 9 after first full refresh seeds the table.
INSERT INTO public.internal_config (key, value)
VALUES ('modal_url_bc_class_precedent_refresher', '')
ON CONFLICT (key) DO NOTHING;
