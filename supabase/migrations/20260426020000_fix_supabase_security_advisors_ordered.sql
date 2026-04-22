-- Ordered Supabase advisor fixes.
--
-- This migration must run after:
--   - `20260424000000_emissions_ledger_foundation.sql`
--   - `20260426000000_candidate_transition_rpc.sql`
--
-- The earlier placeholder migration
-- `20260421233108_fix_supabase_security_advisors.sql` exists only to preserve
-- a version already written to the remote migration history.

ALTER VIEW public.emissions_ledger
  SET (security_invoker = true);

ALTER FUNCTION public.candidate_transition_apply(
  uuid,
  public.candidate_state,
  text,
  text,
  text,
  text,
  text,
  jsonb
)
  SET search_path = public;

DROP POLICY IF EXISTS operator_flags_resolve ON public.operator_flags;
CREATE POLICY operator_flags_resolve ON public.operator_flags
  FOR UPDATE
  TO authenticated
  USING (resolved_at IS NULL)
  WITH CHECK (
    resolved_at IS NOT NULL
    AND resolved_by = (SELECT auth.uid())
  );

DROP POLICY IF EXISTS watchlists_user_rw ON public.watchlists;
CREATE POLICY watchlists_user_rw ON public.watchlists
  FOR ALL
  TO authenticated
  USING ((SELECT auth.uid()) = user_id)
  WITH CHECK ((SELECT auth.uid()) = user_id);

DROP POLICY IF EXISTS notifications_prefs_user_rw ON public.notifications_prefs;
CREATE POLICY notifications_prefs_user_rw ON public.notifications_prefs
  FOR ALL
  TO authenticated
  USING ((SELECT auth.uid()) = user_id)
  WITH CHECK ((SELECT auth.uid()) = user_id);

DROP POLICY IF EXISTS annotations_user_rw ON public.annotations;
CREATE POLICY annotations_user_rw ON public.annotations
  FOR ALL
  TO authenticated
  USING ((SELECT auth.uid()) = user_id)
  WITH CHECK ((SELECT auth.uid()) = user_id);
