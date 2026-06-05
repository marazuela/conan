-- Security: revoke anon write privileges on the bc_* (Light-v4) tables.
--
-- All 19 bc_* tables granted `anon` the full table ACL (arwdDxtm = INSERT, SELECT,
-- UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER). RLS is enabled with authenticated-only
-- SELECT policies and no anon policy, so anon SELECT/INSERT/UPDATE/DELETE already
-- default-deny — BUT TRUNCATE is NOT subject to RLS, so the anon grant let anyone with
-- the public anon key truncate the scoring tables. Revoke all writes (incl TRUNCATE);
-- keep SELECT (RLS-blocked, harmless; the dashboard reads as `authenticated`).
--
-- NOTE: the root cause is a PROJECT-WIDE pg_default_acl that grants anon=arwdDxtm on
-- every new public table (138 tables affected, 119 non-bc). Fixing the default privilege
-- + hardening the other 119 tables is a separate, broader decision (Supabase-platform
-- posture) — tracked outside this migration. This migration hardens the v4 scoring tables.
--
-- Applied live via MCP 2026-06-05; this file keeps disk == live. Idempotent.

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON
  public.bc_application_features,
  public.bc_applications,
  public.bc_candidate_transitions,
  public.bc_candidates_prev,
  public.bc_company_tradeable,
  public.bc_config,
  public.bc_failed_synthesis_calls,
  public.bc_feature_audit,
  public.bc_market_signals,
  public.bc_news_events,
  public.bc_operator_overrides,
  public.bc_pipeline_runs,
  public.bc_prediction_outcomes,
  public.bc_refit_log,
  public.bc_rubric_daily_summary,
  public.bc_rubric_scores,
  public.bc_rubric_versions,
  public.bc_synthesis_audit,
  public.bc_thesis_updates
FROM anon;
