-- Conan — extend catalyst_universe.material_outcome to admit 'negative'.
--
-- Why
-- ---
-- The original three-value domain (yes / no / unclear) treats "materiality" as
-- a single magnitude axis. fda_crl rows are intrinsically directional: a CRL
-- is by definition a *negative-direction* material catalyst (rejection of a
-- pending application, typically -40% to -70% spot moves). Stuffing CRLs into
-- 'yes' loses the direction signal, while 'unclear' is wrong (the materiality
-- IS clear). The companion fetcher
--   modal_workers/fetchers/universe/fda_crl_transparency.py
-- records CRL rows with material_outcome='negative' so downstream coverage
-- auditors + the materiality adjudicator can distinguish the negative class
-- from the positive (fda_approval) class without re-deriving direction from
-- catalyst_type.
--
-- Companion Python validator change lives in
--   modal_workers/shared/emissions_ledger.py (VALID_MATERIAL_OUTCOMES) and
--   modal_workers/scripts/fda_calibration.py (label_from_row + SQL filter).
--
-- Scope
-- -----
-- Drop + re-add the CHECK constraint with the additional value. Idempotent:
-- re-applying the migration drops the old constraint and re-creates the same
-- one with the same value set.

ALTER TABLE public.catalyst_universe
  DROP CONSTRAINT IF EXISTS catalyst_universe_material_outcome_check;

ALTER TABLE public.catalyst_universe
  ADD CONSTRAINT catalyst_universe_material_outcome_check
  CHECK (material_outcome = ANY (ARRAY[
    'yes'::text,
    'no'::text,
    'unclear'::text,
    'negative'::text
  ]));

COMMENT ON COLUMN public.catalyst_universe.material_outcome IS
  'Direction-aware materiality. yes = positive material catalyst (e.g. '
  'fda_approval, phase3_readout success), negative = negative material '
  'catalyst (e.g. fda_crl, withdrawal), no = immaterial (catalyst occurred '
  'but moved the stock <Xσ), unclear = pending price-move classification '
  '(materiality adjudicator will resolve).';
