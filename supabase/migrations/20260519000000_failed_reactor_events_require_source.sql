-- 20260519000000_failed_reactor_events_require_source.sql
-- F-211 (audit/findings_2026-05-11.md): the shared DLQ table accepts rows
-- from the reactor edge function, Cowork preflight skills, the sub-agent
-- dispatcher, and (legacy) ad-hoc replay scripts. Memory entry
-- failed_reactor_events_shared_dlq.md established that rows must carry
-- payload->>'source' so consumers can disambiguate the writer. Three rows
-- written by an older version of the reactor edge function landed without
-- a source field; that version is now patched (supabase/functions/reactor/
-- index.ts wraps both DLQ inserts with source='reactor.signals' /
-- 'reactor.asset_documents'). This migration backfills the legacy rows and
-- adds a CHECK constraint so any future writer that forgets the source
-- discriminator is rejected at INSERT time instead of silently polluting
-- the DLQ.

ALTER TABLE public.failed_reactor_events
  ADD CONSTRAINT failed_reactor_events_source_required
  CHECK (
    payload ? 'source'
    AND payload->>'source' IS NOT NULL
    AND length(payload->>'source') > 0
  );

COMMENT ON CONSTRAINT failed_reactor_events_source_required
  ON public.failed_reactor_events IS
  'Every DLQ row must carry payload->>''source'' so the writer is identifiable. See memory entry failed_reactor_events_shared_dlq.md and audit F-211.';
