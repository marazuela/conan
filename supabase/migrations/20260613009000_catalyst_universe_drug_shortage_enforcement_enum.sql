-- Conan — extend catalyst_universe.catalyst_type to admit 'drug_shortage' + 'drug_enforcement'
--
-- Why
-- ---
-- Two new openFDA-fed universe fetchers land together:
--
--   * modal_workers/fetchers/universe/openfda_drug_shortages.py
--       Pulls active US drug shortages from
--         https://api.fda.gov/drug/shortages.json
--       and upserts catalyst_universe rows at
--         profile='binary_catalyst', catalyst_type='drug_shortage'.
--       Materiality intent: bearish for the brand holder (revenue
--       impairment + manufacturing-quality signal).
--
--   * modal_workers/fetchers/universe/openfda_drug_enforcement.py
--       (sibling spawn) — pulls drug recall / enforcement actions from
--         https://api.fda.gov/drug/enforcement.json
--       at profile='binary_catalyst', catalyst_type='drug_enforcement'.
--
-- The two were spec'd to share a single CHECK-extension migration so
-- they don't race the constraint. This file IS that shared migration —
-- when the drug_enforcement fetcher lands, it relies on this migration
-- already being present (no second CHECK ALTER needed).
--
-- Companion Python validator change lives in
-- modal_workers/shared/emissions_ledger.py — both values added to
-- VALID_CATALYST_TYPES so the helper passes client-side validation
-- before the REST POST.
--
-- Scope
-- -----
-- Add 'drug_shortage' and 'drug_enforcement' to the allowed list.
-- Every other existing value (incl. 'adcomm' from 20260527010000) is
-- preserved. Idempotent: re-applying drops and re-creates the same
-- constraint with the same set of values.

ALTER TABLE public.catalyst_universe
  DROP CONSTRAINT IF EXISTS catalyst_universe_catalyst_type_check;

ALTER TABLE public.catalyst_universe
  ADD CONSTRAINT catalyst_universe_catalyst_type_check
  CHECK (catalyst_type = ANY (ARRAY[
    'fda_approval'::text, 'fda_crl'::text,
    'mna_announce'::text, 'mna_close'::text,
    'activist_13d'::text, 'activist_proxy'::text,
    'short_squeeze_resolved'::text,
    'litigation_verdict'::text,
    'take_private_announce'::text, 'take_private_close'::text,
    'phase3_readout'::text,
    'adcomm'::text,
    'drug_shortage'::text,
    'drug_enforcement'::text
  ]));
