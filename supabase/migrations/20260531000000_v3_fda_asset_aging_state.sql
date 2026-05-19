-- 20260531000000_v3_fda_asset_aging_state.sql
-- v3 port of v2 candidate_aging methodology onto fda_assets.
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M1)
--
-- Additive only. Adds the aging state columns the Stage A SQL sweep + Stage B
-- Cowork skill will read/write. `aging_extensions` is namespaced separately
-- from the existing `extensions` jsonb to avoid collisions with asset-level
-- metadata; specifically holds the `routine_declined` flag (v2 §6.5 / §8a).

ALTER TABLE public.fda_assets
  ADD COLUMN IF NOT EXISTS next_catalyst_date date,
  ADD COLUMN IF NOT EXISTS catalyst_window jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS aging_state text NOT NULL DEFAULT 'watch',
  ADD COLUMN IF NOT EXISTS last_aging_evaluated_at timestamptz,
  ADD COLUMN IF NOT EXISTS aging_extensions jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_name = 'fda_assets_aging_state_check'
  ) THEN
    ALTER TABLE public.fda_assets
      ADD CONSTRAINT fda_assets_aging_state_check
      CHECK (aging_state IN ('watch','active','kill_pending','expired','demoted'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS fda_assets_aging_due_idx
  ON public.fda_assets(last_aging_evaluated_at)
  WHERE is_active = true;

CREATE INDEX IF NOT EXISTS fda_assets_catalyst_window_idx
  ON public.fda_assets(next_catalyst_date)
  WHERE is_active = true;

CREATE INDEX IF NOT EXISTS fda_assets_kill_pending_idx
  ON public.fda_assets(watch_priority, next_catalyst_date)
  WHERE is_active = true AND aging_state = 'kill_pending';

COMMENT ON COLUMN public.fda_assets.aging_state IS
  'v3 aging state machine. watch=passive; active=catalyst within 60d; '
  'kill_pending=catalyst elapsed 1-7d, awaiting Stage B Claude review; '
  'expired=aged out (>60d on watch with no near catalyst); demoted=stale '
  'or catalyst >7d elapsed.';
COMMENT ON COLUMN public.fda_assets.aging_extensions IS
  'v3 aging-specific flags. Holds routine_declined (set by Stage B when '
  'challenger verdict=kill on a kill recommendation); cleared on next '
  'passing Stage B promotion. Namespaced from the existing extensions '
  'column to avoid collision with asset-level metadata.';
COMMENT ON COLUMN public.fda_assets.next_catalyst_date IS
  'v3 aging: nearest known catalyst date (pdufa_date / adcom_vote / etc.). '
  'Refreshed by Stage A from extracted_facts. Used by 60d / 7d window rules.';
COMMENT ON COLUMN public.fda_assets.catalyst_window IS
  'v3 aging: fuzzy catalyst window (jsonb {lower, upper}) when an exact date '
  'is unknown. Mirrors v2 candidates.next_catalyst_window daterange semantics.';
