-- v3 content-aware enqueue dedup — PR-2 of the cross-cutting orchestrator fix.
--
-- Problem: AXSM was scored 17 times in 96h. All 17 cluster around the same
-- evidence set; cross_source events fired the reactor on every doc co-occurrence
-- but the underlying material primary-document set was unchanged. The reactor
-- has no content-aware dedup — only an (asset, trigger_type, trigger_doc_id)
-- partial unique index that drops once pending → running.
--
-- Fix: stamp every orchestrator_runs / convergence_assessments row with a
-- `document_set_hash` over the asset's material primary asset_documents at
-- enqueue time. The reactor checks the latest non-superseded
-- convergence_assessments row; if the hash matches, skip the new enqueue
-- (unless trigger_type is in the bypass set — operator_refresh, manual,
-- tier2_escalation, catalyst_proximity, aging_recheck).
--
-- DB-side belt-and-suspenders: a NEW partial unique index on
-- (asset_id, document_set_hash) WHERE status='pending' AND trigger_type
-- IN ('new_doc','cross_source','market_move') prevents simultaneous reactor
-- invocations from inserting duplicate content-equivalent pending rows.
--
-- Rollback:
--   alter table orchestrator_runs drop column if exists document_set_hash;
--   alter table convergence_assessments drop column if exists document_set_hash;
--   drop index if exists orchestrator_runs_pending_content_dedup_idx;
--
-- Sequencing: PR-2. Lands after PR-3 (catalyst-proximity sweeper, which
-- needs the trigger_type CHECK extension) and before PR-1 (seed activation,
-- which wants the noise floor in place first).

-- ---------------------------------------------------------------------------
-- 1. Add document_set_hash columns. Nullable (legacy rows have no hash).
-- ---------------------------------------------------------------------------

alter table public.orchestrator_runs
  add column if not exists document_set_hash text;

alter table public.convergence_assessments
  add column if not exists document_set_hash text;

comment on column public.orchestrator_runs.document_set_hash is
  'md5 of sorted material primary asset_documents.document_id at enqueue time. NULL on legacy rows. Used by reactor for content-aware dedup.';

comment on column public.convergence_assessments.document_set_hash is
  'md5 of sorted material primary asset_documents.document_id at synthesis time. Persisted by runtime.stage_10_persist for the next enqueue to compare against.';

-- ---------------------------------------------------------------------------
-- 2. Content-aware partial unique index. Only applies to the doc-bus triggers
--    (new_doc, cross_source, market_move). Bypass triggers (manual,
--    operator_refresh, tier2_escalation, catalyst_proximity, aging_recheck,
--    scheduled, backtest) intentionally allowed to enqueue regardless.
-- ---------------------------------------------------------------------------

-- Index is intentionally restricted to rows where document_set_hash IS NOT NULL.
-- The reactor stamps the hash at enqueue time for all new doc-bus rows; legacy
-- pending rows from before this migration carry NULL hashes and are
-- grandfathered (the existing orchestrator_runs_pending_dedup_idx still
-- enforces the older (asset, type, doc) dedup on them). Once the drain
-- works through the legacy pending backlog, all live pending rows will be
-- the new shape and the content-dedup index covers them.
create unique index if not exists orchestrator_runs_pending_content_dedup_idx
  on public.orchestrator_runs
    (asset_id, document_set_hash)
  where status = 'pending'
    and document_set_hash is not null
    and trigger_type in ('new_doc','cross_source','market_move');

comment on index public.orchestrator_runs_pending_content_dedup_idx is
  'Race-safe dedup for doc-bus enqueues: if a pending row already covers the same (asset_id, document_set_hash), suppress duplicate. Application-side check in reactor catches the cross-run case (last completed row had same hash); this index catches the cross-webhook case (two simultaneous inserts). Legacy pending rows with NULL hash are exempt — they predate the content-dedup contract.';
