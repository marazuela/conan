-- v3 over-firing fix PR-A: SQL-side document_set_hash helper.
--
-- Mirror of orchestrator_runtime/tier2.py::compute_document_set_hash and
-- supabase/functions/reactor/index.ts::computeDocSetHash so pg_cron enqueue
-- paths (catalyst_proximity, and any future SQL-only sweepers) can stamp the
-- same hash the Python and Deno paths produce.
--
-- Hash definition: md5 of comma-separated material primary
-- asset_documents.document_id values, sorted lexicographically by uuid text.
-- Matches Python's `hashlib.md5(",".join(sorted(doc_ids)))` and Deno's
-- `sorted.join(",")` then MD5 (with SHA-256 fallback truncated to 32 chars
-- on runtimes that don't expose MD5).
--
-- Returns NULL when an asset has zero material primary docs (matches both
-- Python `if not doc_ids: return None` and Deno `if data.length === 0
-- return null`). NULL is treated as "no content fingerprint" by the
-- partial unique index on (asset_id, document_set_hash) and by the reactor's
-- application-side dedup check.
--
-- Rollback: drop function if exists public.compute_document_set_hash_sql(uuid);

create or replace function public.compute_document_set_hash_sql(
  p_asset_id uuid
) returns text
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
  select md5(string_agg(document_id::text, ',' order by document_id::text))
    from public.asset_documents
   where asset_id = p_asset_id
     and link_type = 'primary'
     and is_material is true;
$$;

comment on function public.compute_document_set_hash_sql(uuid) is
  'md5 of sorted material primary asset_documents.document_id for the asset. '
  'Mirrors Python compute_document_set_hash (orchestrator_runtime/tier2.py) and '
  'Deno computeDocSetHash (reactor/index.ts). Returns NULL when the asset has '
  'no material primary docs. Used by pg_cron-only enqueue paths (e.g. '
  'v3-catalyst-proximity-sweep) so their orchestrator_runs inserts can populate '
  'document_set_hash and participate in the partial unique dedup index '
  'orchestrator_runs_pending_content_dedup_idx.';
