-- Conan — extend catalyst_universe.catalyst_type to admit 'adcomm'
--
-- Why
-- ---
-- modal_workers/sub_agents/regulatory_history.py exposes two MCP tools
-- (fda_adcomm_upcoming, fda_adcomm_historical) that query
--   `catalyst_universe WHERE catalyst_type = 'adcomm'`
-- but the existing CHECK constraint
--   catalyst_universe_catalyst_type_check
-- doesn't include 'adcomm', so the table holds zero such rows and the
-- MCP always returns empty.
--
-- The companion producer is the new fetcher
-- modal_workers/fetchers/universe/fed_register_adcom.py, which pulls FDA
-- Advisory Committee meeting notices from the Federal Register API,
-- parses meeting dates, and upserts one row per meeting at
-- profile='binary_catalyst', catalyst_type='adcomm'. It needs this
-- enum extension to land.
--
-- Companion Python validator change lives in
-- modal_workers/shared/emissions_ledger.py — 'adcomm' added to
-- VALID_CATALYST_TYPES so the helper passes server-side validation
-- before the REST POST.
--
-- Scope
-- -----
-- Add 'adcomm' to the allowed list. Every other existing value is
-- preserved.  Idempotent: re-applying drops and re-creates the same
-- constraint with the same set of values plus 'adcomm'.

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
    'adcomm'::text
  ]));
