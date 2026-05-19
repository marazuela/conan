-- One-shot: collapse pre-2026-05-11 EDGAR duplicate-bucket DLQs.
--
-- Context: before the (adsh, ticker) dedup landed in
-- `modal_workers/scanners/edgar_filing_monitor.py`, a single EDGAR filing
-- matching N keyword buckets emitted N signals → N thesis_jobs. The AI
-- routine (thesis_writer) then declined each one with
-- gate_reasons[0] = 'routine_declined: ... Duplicate keyword variants.'
-- burning one routine run per duplicate before reaching that verdict.
--
-- This script keeps one canonical thesis_job per (accession_number) and
-- marks the rest resolved (resolved_at = now(), gate_reasons annotated).
-- Canonical row = the lowest `id` within the duplicate set (i.e. the row
-- that arrived first — also matches the order of insertion the rest of the
-- pipeline already cited in its observability output).
--
-- The duplicates live entirely within `signal_id` strings of the form
--   edgar_<accession>_<bucket>_<keyword_hash>
-- where <bucket> ∈ (mna|distress|governance|activist)_keyword. We extract
-- the accession out of signal_id via a regex so the script works even if
-- thesis_jobs doesn't carry an accession column directly.
--
-- Safety:
--   * Only touches rows with status='dlq' AND resolved_at IS NULL.
--   * Only touches rows whose signal_id matches the bucketed regex below.
--   * Wrapped in a transaction with a COMMIT at the end — change to ROLLBACK
--     for a dry run.
--   * Includes a verification SELECT before COMMIT so the operator sees
--     exactly which rows would change.

BEGIN;

WITH dup_groups AS (
    SELECT
        id,
        signal_id,
        substring(signal_id FROM '^edgar_(\d+)_(?:mna|distress|governance|activist)_keyword_') AS accession,
        row_number() OVER (
            PARTITION BY substring(signal_id FROM '^edgar_(\d+)_(?:mna|distress|governance|activist)_keyword_')
            ORDER BY id
        ) AS rn
    FROM public.thesis_jobs
    WHERE status = 'dlq'
      AND resolved_at IS NULL
      AND signal_id ~ '^edgar_\d+_(?:mna|distress|governance|activist)_keyword_'
)
SELECT
    accession,
    count(*) AS group_size,
    count(*) FILTER (WHERE rn > 1) AS will_resolve,
    min(id) FILTER (WHERE rn = 1) AS canonical_id,
    array_agg(id ORDER BY id) AS all_ids
FROM dup_groups
GROUP BY accession
HAVING count(*) > 1
ORDER BY accession;

-- Resolve the non-canonical duplicates. Append a synthetic gate-reasons
-- entry so downstream observability ('why was this DLQ resolved?') keeps
-- a clear trail.
WITH dup_groups AS (
    SELECT
        id,
        substring(signal_id FROM '^edgar_(\d+)_(?:mna|distress|governance|activist)_keyword_') AS accession,
        row_number() OVER (
            PARTITION BY substring(signal_id FROM '^edgar_(\d+)_(?:mna|distress|governance|activist)_keyword_')
            ORDER BY id
        ) AS rn
    FROM public.thesis_jobs
    WHERE status = 'dlq'
      AND resolved_at IS NULL
      AND signal_id ~ '^edgar_\d+_(?:mna|distress|governance|activist)_keyword_'
),
to_resolve AS (
    SELECT id FROM dup_groups WHERE rn > 1
)
UPDATE public.thesis_jobs t
SET
    resolved_at = now(),
    gate_reasons = COALESCE(t.gate_reasons, '[]'::jsonb)
        || jsonb_build_array(
            'backfill_2026_05_11_edgar_dedup: collapsed duplicate keyword variant on same (adsh, ticker); '
            || 'see PR titled "edgar scanner: dedup signals by (accession, ticker)"'
        )
FROM to_resolve
WHERE t.id = to_resolve.id;

-- Verification: should report zero remaining duplicate accessions.
SELECT
    substring(signal_id FROM '^edgar_(\d+)_(?:mna|distress|governance|activist)_keyword_') AS accession,
    count(*) AS remaining_dlqs
FROM public.thesis_jobs
WHERE status = 'dlq'
  AND resolved_at IS NULL
  AND signal_id ~ '^edgar_\d+_(?:mna|distress|governance|activist)_keyword_'
GROUP BY accession
HAVING count(*) > 1;

COMMIT;
