-- Conan v2 — harden rubric version contract.
--
-- Scoring math is currently implemented in modal_workers/shared/rubric_engine.py.
-- Signal ingest now stamps the exact rubric_version implemented by that code, but
-- the database should still prevent two simultaneously-active rubric rows for the
-- same profile. Before adding the partial unique index, collapse any accidental
-- active duplicates by keeping the newest/highest version active.

WITH ranked AS (
  SELECT
    id,
    row_number() OVER (
      PARTITION BY profile
      ORDER BY rubric_version DESC, effective_at DESC, id DESC
    ) AS rn
  FROM public.rubrics
  WHERE superseded_at IS NULL
)
UPDATE public.rubrics r
SET superseded_at = now()
FROM ranked
WHERE r.id = ranked.id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS rubrics_one_active_per_profile_idx
  ON public.rubrics(profile)
  WHERE superseded_at IS NULL;
