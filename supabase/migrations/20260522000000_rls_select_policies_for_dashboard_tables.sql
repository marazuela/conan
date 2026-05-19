-- Restore dashboard visibility for 9 tables that had RLS enabled but no SELECT policy.
-- Without a policy, RLS denies-by-default, so the authenticated session reads zero rows
-- even though grants permit SELECT. This matches the existing select-true pattern used
-- by signals, thesis_jobs, fda_assets, operator_flags, candidates, entities, alerts,
-- candidate_aging_failures, and fda_regulatory_events.

CREATE POLICY asset_documents_select ON public.asset_documents
  FOR SELECT TO authenticated USING (true);

CREATE POLICY asset_linker_runs_select ON public.asset_linker_runs
  FOR SELECT TO authenticated USING (true);

CREATE POLICY convergence_assessments_select ON public.convergence_assessments
  FOR SELECT TO authenticated USING (true);

CREATE POLICY documents_select ON public.documents
  FOR SELECT TO authenticated USING (true);

CREATE POLICY eval_harness_select ON public.eval_harness
  FOR SELECT TO authenticated USING (true);

CREATE POLICY eval_runs_select ON public.eval_runs
  FOR SELECT TO authenticated USING (true);

CREATE POLICY extracted_facts_select ON public.extracted_facts
  FOR SELECT TO authenticated USING (true);

CREATE POLICY fda_asset_parties_select ON public.fda_asset_parties
  FOR SELECT TO authenticated USING (true);

CREATE POLICY orchestrator_runs_select ON public.orchestrator_runs
  FOR SELECT TO authenticated USING (true);
