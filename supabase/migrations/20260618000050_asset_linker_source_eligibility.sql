-- asset_linker source eligibility — derive which documents.source values are
-- worth classifying from the live fda_assets.program_status mix, replacing the
-- hardcoded SOURCE_ALLOWLIST = ('clinicaltrials',) constant in
-- modal_workers/extractor/asset_linker.py (introduced PR #52). See issue #54.
--
-- WHY: gold-set evidence (2026-05-13, 500 docs) showed 100% of real links in a
-- trial-stage universe come from clinicaltrials; the other sources contributed
-- 0/425. That is correct *today* but wrong the moment a post-approval asset
-- enters fda_assets (dailymed labels, openfda pharmacovigilance, federal_register
-- AdCom notices all become real link sources). A hardcoded constant needs a
-- code+deploy every time the universe shape shifts. This table makes source
-- eligibility a *data* decision keyed off program_status, so onboarding an
-- approved asset auto-widens the eligible sources with no code change.
--
-- IMPORTANT — seed uses the LIVE program_status taxonomy, NOT the hypothetical
-- one in the issue body. The issue proposed preclinical/phase_1/.../under_review,
-- but production fda_assets only ever carries: NULL, 'phase2', 'phase3', 'filed',
-- 'approved' (verified 2026-06-05). Seeding the issue's values verbatim would
-- match ZERO active assets and the linker would go dark. The seed below maps the
-- real values; the watchdog (20260618000060) flags any future status the table
-- does not yet cover.

-- ---------------------------------------------------------------------------
-- Rule table: (program_status, source) pairs that are worth classifying.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.asset_linker_source_eligibility (
  program_status text NOT NULL,   -- matches fda_assets.program_status; '_unset'
                                  -- is a sentinel for NULL/'' status (see view).
  source         text NOT NULL,   -- matches documents.source
  notes          text,
  PRIMARY KEY (program_status, source)
);

COMMENT ON TABLE public.asset_linker_source_eligibility IS
  'Drives asset_linker source eligibility from fda_assets.program_status. A '
  'documents.source is eligible for pass-1 classification iff some active '
  'fda_asset has a program_status row pairing it with that source (resolved by '
  'the asset_linker_eligible_sources view). Replaces the hardcoded '
  'SOURCE_ALLOWLIST constant — issue #54. Add a row to onboard a new '
  '(status, source) pair without a code change.';
COMMENT ON COLUMN public.asset_linker_source_eligibility.program_status IS
  'Matches fda_assets.program_status. The literal ''_unset'' is a sentinel that '
  'matches assets whose program_status IS NULL or empty (via COALESCE in the '
  'eligible-sources view + the orphan watchdog), keeping clinicaltrials eligible '
  'for not-yet-staged assets.';

-- Service-role-only, matching peer operational tables (doc_asset_candidates,
-- fda_asset_aliases, internal_config): RLS on, no policies. The linker reads via
-- the service-role key, which bypasses RLS.
ALTER TABLE public.asset_linker_source_eligibility ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Seed (idempotent). Trial-stage statuses → clinicaltrials only; post-approval
-- statuses → labels + pharmacovigilance + AdCom notices. edgar is deliberately
-- excluded (gold set: 0/100 positives, dominated by structured-finance filings).
-- ---------------------------------------------------------------------------
INSERT INTO public.asset_linker_source_eligibility (program_status, source, notes) VALUES
  -- Trial-stage (live values): only clinicaltrials.gov overlaps the universe.
  ('phase2',         'clinicaltrials', 'trial-stage'),
  ('phase3',         'clinicaltrials', 'trial-stage'),
  -- 'filed' = NDA/BLA filed, under FDA review. Kept clinicaltrials-only on
  -- purpose: enabling federal_register here would turn AdCom-source docs on for
  -- the 6 active filed assets and break the "clinicaltrials-only until an
  -- approved asset exists" invariant. Add ('filed','federal_register') later if
  -- AdCom coverage for under-review assets is wanted.
  ('filed',          'clinicaltrials', 'NDA/BLA filed, under review'),
  -- Sentinel for NULL/'' program_status (29 active assets as of 2026-06-05).
  -- Keeps clinicaltrials eligible for not-yet-staged assets and stops the
  -- orphan watchdog from flagging NULL as an unknown status.
  ('_unset',         'clinicaltrials', 'NULL/empty program_status sentinel'),
  -- Post-approval: labels (dailymed), pharmacovigilance (openfda), AdCom /
  -- regulatory notices (federal_register). clinicaltrials stays on for Phase-4
  -- / post-marketing trials.
  ('approved',       'clinicaltrials',   'post-approval'),
  ('approved',       'dailymed',         'post-approval labels'),
  ('approved',       'openfda',          'post-approval pharmacovigilance'),
  ('approved',       'federal_register', 'post-approval AdCom / notices'),
  ('post_marketing', 'clinicaltrials',   'post-marketing trials'),
  ('post_marketing', 'dailymed',         'post-marketing labels'),
  ('post_marketing', 'openfda',          'post-marketing pharmacovigilance'),
  ('post_marketing', 'federal_register', 'post-marketing notices')
ON CONFLICT (program_status, source) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Resolved dynamic allowlist: the DISTINCT set of sources eligible for the
-- current active universe. asset_linker.load_eligible_sources() reads this.
-- COALESCE(NULLIF(...,''),'_unset') folds NULL/empty program_status onto the
-- sentinel so those assets resolve to clinicaltrials.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.asset_linker_eligible_sources AS
SELECT DISTINCT e.source
  FROM public.fda_assets a
  JOIN public.asset_linker_source_eligibility e
    ON e.program_status = COALESCE(NULLIF(a.program_status, ''), '_unset')
 WHERE a.is_active = true;

COMMENT ON VIEW public.asset_linker_eligible_sources IS
  'DISTINCT documents.source values worth classifying for the current active '
  'fda_assets universe, resolved from asset_linker_source_eligibility. Read by '
  'asset_linker.load_eligible_sources() to build the pass-1 source filter '
  '(replaces SOURCE_ALLOWLIST). Empty result => caller falls back to '
  'clinicaltrials to avoid a dark linker. Issue #54.';
