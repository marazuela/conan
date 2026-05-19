-- archive_v2 read RPCs for the conan-dashboard /operator/archive page.
--
-- Background:
-- Phase 1 v2 teardown (20260511150239) intentionally locked the archive_v2
-- schema to service_role only — no anon/authenticated GRANTs, schema not
-- exposed via PostgREST. The dashboard at /operator/archive (Next.js app,
-- separate repo marazuela/conan-dashboard) is a server component that uses
-- the Supabase anon client (cookie-derived JWT) and calls
-- `.schema('archive_v2').from(<table>)` to read these tables. That request
-- fails: schema not exposed + role lacks USAGE → "schema must be one of the
-- following" / 42501 permission denied. The page renders the Next.js error
-- boundary; the same page is the redirect target for /convergence,
-- /profiles, /archive, /reports (see conan-dashboard/next.config.ts), so
-- five live URLs 500.
--
-- Two reasonable fix shapes were considered:
--   (a) GRANT USAGE/SELECT to authenticated + add archive_v2 to Supabase API
--       "Exposed schemas". Pros: simple; lets supabase-js .schema() work.
--       Cons: Studio-config change is not migration-tracked; widens the
--       blast radius (all authenticated rows readable, not just on the
--       single audit page); contradicts the explicit Phase-1 intent.
--   (b) public.* SECURITY DEFINER RPCs that wrap archive_v2 reads. Pros:
--       fully migration-tracked, no Studio config change, keeps archive_v2
--       schema-level lockdown intact, narrow API surface. Cons: one extra
--       indirection in the dashboard data layer.
--
-- We went with (b). The two functions below are the entire read surface
-- needed by /operator/archive — `archive_v2_counts()` for the tab header
-- counts and `archive_v2_list(table, limit, offset)` for paginated table
-- listings. Both are SECURITY DEFINER (run as the owning postgres role,
-- which has USAGE on archive_v2) and GRANT EXECUTE to authenticated.
--
-- archive_v2_list whitelists table names by name (no dynamic SQL injection
-- vector — table identifier comes from a CASE branch, not from the input
-- string interpolated into the query).
--
-- Rollback: DROP FUNCTION public.archive_v2_counts(),
--          DROP FUNCTION public.archive_v2_list(text, int, int);


-- ===========================================================================
-- 1) archive_v2_counts() — header tab counts (6 entries)
-- ===========================================================================

CREATE OR REPLACE FUNCTION public.archive_v2_counts()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, archive_v2
AS $function$
DECLARE
  v_result jsonb;
BEGIN
  -- gate: any authenticated user; route-level RBAC happens in Next middleware.
  IF auth.uid() IS NULL THEN
    RAISE EXCEPTION 'archive_v2_counts: not authenticated'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  SELECT jsonb_build_object(
    'candidates',  (SELECT count(*) FROM archive_v2.candidates),
    'signals',     (SELECT count(*) FROM archive_v2.signals),
    'thesis_jobs', (SELECT count(*) FROM archive_v2.thesis_jobs),
    'alerts',      (SELECT count(*) FROM archive_v2.alerts),
    'outcomes',    (SELECT count(*) FROM archive_v2.outcomes),
    'rubrics',     (SELECT count(*) FROM archive_v2.rubrics)
  )
  INTO v_result;

  RETURN v_result;
END;
$function$;

COMMENT ON FUNCTION public.archive_v2_counts() IS
  'Row counts for the 6 archive_v2 tables surfaced by the dashboard /operator/archive page. SECURITY DEFINER so anon/authenticated clients can read the counts without GRANTs on the archive_v2 schema itself.';


-- ===========================================================================
-- 2) archive_v2_list(p_table, p_limit, p_offset) — paginated table listing
-- ===========================================================================
-- Returns ALL columns of the requested archive_v2 table as a jsonb array,
-- plus the unfiltered total. The dashboard narrows at the call site via
-- the typed Row shape (Database['archive_v2']['Tables'][T]['Row']).
--
-- Order-by per table is hardcoded to match the existing page contract:
--   candidates: updated_at DESC
--   signals:    scan_date DESC
--   thesis_jobs:created_at DESC
--   alerts:     dispatched_at DESC NULLS LAST
--   outcomes:   labeled_at DESC NULLS LAST
--   rubrics:    effective_at DESC

CREATE OR REPLACE FUNCTION public.archive_v2_list(
  p_table  text,
  p_limit  int DEFAULT 100,
  p_offset int DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, archive_v2
AS $function$
DECLARE
  v_rows  jsonb;
  v_total bigint;
  v_limit int := LEAST(GREATEST(p_limit, 1), 500);
  v_offset int := GREATEST(p_offset, 0);
BEGIN
  IF auth.uid() IS NULL THEN
    RAISE EXCEPTION 'archive_v2_list: not authenticated'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  -- Whitelist table name. CASE branches use static identifiers so the
  -- p_table string can never be interpolated into SQL.
  CASE p_table
    WHEN 'candidates' THEN
      SELECT count(*) INTO v_total FROM archive_v2.candidates;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.updated_at DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.candidates
           ORDER BY updated_at DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    WHEN 'signals' THEN
      SELECT count(*) INTO v_total FROM archive_v2.signals;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.scan_date DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.signals
           ORDER BY scan_date DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    WHEN 'thesis_jobs' THEN
      SELECT count(*) INTO v_total FROM archive_v2.thesis_jobs;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.created_at DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.thesis_jobs
           ORDER BY created_at DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    WHEN 'alerts' THEN
      SELECT count(*) INTO v_total FROM archive_v2.alerts;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.dispatched_at DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.alerts
           ORDER BY dispatched_at DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    WHEN 'outcomes' THEN
      SELECT count(*) INTO v_total FROM archive_v2.outcomes;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.labeled_at DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.outcomes
           ORDER BY labeled_at DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    WHEN 'rubrics' THEN
      SELECT count(*) INTO v_total FROM archive_v2.rubrics;
      SELECT coalesce(jsonb_agg(to_jsonb(t.*) ORDER BY t.effective_at DESC NULLS LAST), '[]'::jsonb)
        INTO v_rows
        FROM (
          SELECT * FROM archive_v2.rubrics
           ORDER BY effective_at DESC NULLS LAST
           OFFSET v_offset LIMIT v_limit
        ) t;

    ELSE
      RAISE EXCEPTION 'archive_v2_list: unknown table %, must be one of (candidates,signals,thesis_jobs,alerts,outcomes,rubrics)', p_table
        USING ERRCODE = 'invalid_parameter_value';
  END CASE;

  RETURN jsonb_build_object('rows', v_rows, 'total', v_total);
END;
$function$;

COMMENT ON FUNCTION public.archive_v2_list(text, int, int) IS
  'Paginated row listing for archive_v2 tables surfaced by the dashboard /operator/archive page. Returns jsonb {"rows": [...], "total": N}. Whitelists table name via CASE branch (no dynamic SQL).';


-- ===========================================================================
-- 3) GRANTs — authenticated only (anon stays locked out)
-- ===========================================================================

REVOKE ALL ON FUNCTION public.archive_v2_counts() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.archive_v2_list(text, int, int) FROM PUBLIC, anon;

GRANT EXECUTE ON FUNCTION public.archive_v2_counts() TO authenticated;
GRANT EXECUTE ON FUNCTION public.archive_v2_list(text, int, int) TO authenticated;
