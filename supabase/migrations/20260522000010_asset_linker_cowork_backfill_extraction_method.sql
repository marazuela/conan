-- Allow the new Cowork-resident asset_linker_backfill skill to emit asset_documents
-- rows and run-log entries without violating the existing extraction_method /
-- asset_linker_runs.pass CHECK constraints. 'cowork_backfill' is distinct from
-- 'agent_pass1' / 'agent_pass2' / 'manual' so the dashboard, watchdog, and the
-- Modal pass-2 verifier can filter Cowork-emitted rows out of their workflows.

ALTER TABLE public.asset_documents
  DROP CONSTRAINT asset_documents_extraction_method_check;

ALTER TABLE public.asset_documents
  ADD CONSTRAINT asset_documents_extraction_method_check
  CHECK (extraction_method = ANY (ARRAY[
    'regex'::text,
    'ner'::text,
    'agent_pass1'::text,
    'agent_pass2'::text,
    'manual'::text,
    'cowork_backfill'::text
  ]));

ALTER TABLE public.asset_linker_runs
  DROP CONSTRAINT asset_linker_runs_pass_check;

ALTER TABLE public.asset_linker_runs
  ADD CONSTRAINT asset_linker_runs_pass_check
  CHECK (pass = ANY (ARRAY[
    'pass1'::text,
    'pass2'::text,
    'cowork_backfill'::text
  ]));
