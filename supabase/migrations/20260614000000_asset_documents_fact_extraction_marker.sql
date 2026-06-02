-- Add an attempted-marker column to asset_documents so fact_extractor_opus can
-- stamp every doc it touches, including docs that legitimately yield zero facts
-- under the "Honest Empty" invariant.
--
-- Background (2026-06-01 investigation, operator_flag 58a920d2-…-9988e68d5b65):
-- The fact_extractor_opus skill's pending-work query was a NOT EXISTS clause
-- against extracted_facts. That clause cannot distinguish "doc not yet visited"
-- from "doc visited, no investor-grade facts found" — so scanner-stub docs from
-- the conan_signal source (eop2_meeting / pre_phase3_readout / pdufa_watchlist
-- with ~600-2200 char JSON payloads, frequently zero extractable facts) stay in
-- the queue forever and the watchdog's last-extracted-fact freshness probe
-- false-alarms even when the skill is correctly returning honest-empty runs.
--
-- This mirrors the asset_linker discipline (documents.linker_classified_at).

ALTER TABLE public.asset_documents
  ADD COLUMN IF NOT EXISTS fact_extraction_attempted_at timestamptz;

COMMENT ON COLUMN public.asset_documents.fact_extraction_attempted_at IS
  'Stamped by fact_extractor_opus on every doc touched — including honest-empty '
  '(zero facts emitted) runs. Without this stamp, scanner-stub docs that '
  'legitimately yield zero facts re-queue forever and dark-flag the skill.';

-- Partial index for the queue query: WHERE is_material AND attempted_at IS NULL
-- ORDER BY created_at DESC LIMIT 10. The partial predicate keeps the index
-- small (it only contains docs that still need work).
CREATE INDEX IF NOT EXISTS asset_documents_fact_extract_pending_idx
  ON public.asset_documents (created_at DESC)
  WHERE is_material = true AND fact_extraction_attempted_at IS NULL;

-- Backfill: any (document_id, asset_id) pair that already has at least one
-- extracted_facts row is implicitly "attempted" — stamp it with the latest
-- known extraction time. This prevents the post-deploy first run from
-- re-claiming the entire historical backlog.
UPDATE public.asset_documents ad
   SET fact_extraction_attempted_at = sub.last_extract
  FROM (
    SELECT document_id, asset_id, max(extracted_at) AS last_extract
      FROM public.extracted_facts
     GROUP BY document_id, asset_id
  ) sub
 WHERE sub.document_id = ad.document_id
   AND sub.asset_id    = ad.asset_id
   AND ad.fact_extraction_attempted_at IS NULL;
