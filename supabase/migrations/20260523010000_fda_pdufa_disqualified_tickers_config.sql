-- F-316: seed the fda_pdufa_disqualified_tickers internal_config key with
-- the current hardcoded values from fda_pdufa_pipeline.py:DISQUALIFIED_TICKERS.
--
-- Background
-- ----------
-- The DISQUALIFIED_TICKERS dict has lived in fda_pdufa_pipeline.py since v1
-- (D-039 in DECISIONS.md). Module comment says "Pedro edits this in-place" —
-- meaning every change requires a code edit + Modal redeploy + audit-trail
-- recovery via git log. That's high friction and forces a deploy on every
-- "oh, this ticker just got approved, stop scoring its PDUFA".
--
-- This migration mirrors the current values into internal_config (which is
-- already the canonical place for operator-editable runtime config — see
-- modal_url_* keys for the same pattern). The Python code (separate commit)
-- reads from internal_config at scan start, falling back to the in-code dict
-- if the load fails (network blip, key missing).
--
-- Operator edit pattern post-migration:
--   UPDATE public.internal_config
--      SET value = '{"NEWTICKER": "reason", ...}'
--    WHERE key = 'fda_pdufa_disqualified_tickers';
-- No redeploy. updated_at provides the audit trail.

INSERT INTO public.internal_config (key, value)
VALUES (
  'fda_pdufa_disqualified_tickers',
  jsonb_build_object(
    'ZLAB', 'Augtyro already FDA-approved (Jun 2024). Scanner picks up China NMPA milestones.',
    'CORT', 'Relacorilant (Lifyorli) approved early Mar 25, 2026. Not a pending PDUFA.',
    'ORCA', 'Private company, not publicly traded. Cannot be actioned.'
  )::text
)
ON CONFLICT (key) DO NOTHING;
