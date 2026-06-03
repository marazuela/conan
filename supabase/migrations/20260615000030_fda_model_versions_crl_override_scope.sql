-- =============================================================================
-- Allow the CRL rubric's fair_probability override (Seam 2) to be activated as a
-- logged, reversible fda_model_versions row. Adds 'fda_crl_override' to the scope
-- whitelist. The bridge (fda_signal_bridge._resolve_crl_override_enabled) treats
-- an active row of this scope (effective_at set, superseded_at null) as
-- "override ON", unless FDA_CRL_OVERRIDE_ENABLED explicitly forces on/off.
--
-- Managed via modal_workers/scripts/fda_crl_override_admin.py (--enable/--disable).
-- =============================================================================

ALTER TABLE public.fda_model_versions
  DROP CONSTRAINT IF EXISTS fda_model_versions_scope_check;

ALTER TABLE public.fda_model_versions
  ADD CONSTRAINT fda_model_versions_scope_check
  CHECK (scope = ANY (ARRAY['priors'::text, 'thresholds'::text, 'both'::text, 'fda_crl_override'::text]));
