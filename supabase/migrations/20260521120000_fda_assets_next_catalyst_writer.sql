-- Conan — populate fda_assets.next_catalyst_date from upstream evidence.
--
-- Why
-- ---
-- fda_assets.next_catalyst_date is read by v3 aging buckets
-- (20260531000030_v3_fda_aging_sql_functions.sql) and by catalyst-proximity
-- targeting in bulk_orchestrator, but the only writers today are the legacy
-- v2 operator-promote RPC (fda_event_approve_for_thesis) and the dedup-merge
-- path. Net effect: ~1/62 active assets carry a populated date and the
-- catalyst-proximity sweep selects on essentially no signal.
--
-- This migration adds a deterministic recompute function fed by triggers on
-- the two upstream sources of truth:
--   * extracted_facts (fact_type='pdufa_date') — Sonnet extractor output per
--     document
--   * catalyst_universe (FDA-flavored catalyst_types) — fetcher-fed ledger
--     with one row per upcoming event
-- The recompute picks the earliest future date across both sources. It only
-- moves the column to a sooner date (or fills NULL); a later upstream signal
-- never regresses an already-known earlier catalyst.
--
-- Idempotent. A backfill helper at the bottom replays over all active rows.

-- ---------------------------------------------------------------------------
-- 1) Recompute helper. Single-asset, safe to call repeatedly.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_assets_recompute_next_catalyst_date(
  p_asset_id uuid
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_entity_id uuid;
  v_current_value date;
  v_from_facts date;
  v_from_universe date;
  v_candidate date;
BEGIN
  SELECT entity_id, next_catalyst_date
    INTO v_entity_id, v_current_value
    FROM public.fda_assets
   WHERE id = p_asset_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;

  -- Earliest future PDUFA date extracted from any document linked to this asset.
  -- fact_text is free-form; require ISO-8601 YYYY-MM-DD before casting.
  SELECT MIN(ef.fact_text::date)
    INTO v_from_facts
    FROM public.extracted_facts ef
   WHERE ef.asset_id = p_asset_id
     AND ef.fact_type = 'pdufa_date'
     AND ef.fact_text ~ '^\d{4}-\d{2}-\d{2}$'
     AND ef.fact_text::date >= current_date;

  -- Earliest future FDA catalyst recorded in catalyst_universe for the
  -- asset's owning entity. Restricted to the FDA-flavored catalyst_types so
  -- M&A and litigation events on the same entity don't pollute the column.
  IF v_entity_id IS NOT NULL THEN
    SELECT MIN(cu.catalyst_date)
      INTO v_from_universe
      FROM public.catalyst_universe cu
     WHERE cu.entity_id = v_entity_id
       AND cu.catalyst_type IN ('fda_approval','fda_crl','phase3_readout','adcomm')
       AND cu.catalyst_date >= current_date;
  END IF;

  v_candidate := LEAST(v_from_facts, v_from_universe);

  -- LEAST(NULL, x) = NULL on some configurations; explicitly fall back when
  -- one side is null so the surviving side still wins.
  IF v_candidate IS NULL THEN
    v_candidate := COALESCE(v_from_facts, v_from_universe);
  END IF;

  IF v_candidate IS NULL THEN
    RETURN;
  END IF;

  -- Only move sooner. Never regress to a later date — a delayed PDUFA gets
  -- handled by an explicit operator write, not by this auto-populator.
  IF v_current_value IS NULL OR v_candidate < v_current_value THEN
    UPDATE public.fda_assets
       SET next_catalyst_date = v_candidate,
           updated_at = now()
     WHERE id = p_asset_id
       AND (next_catalyst_date IS NULL OR next_catalyst_date > v_candidate);
  END IF;
END;
$$;

COMMENT ON FUNCTION public.fda_assets_recompute_next_catalyst_date(uuid) IS
  'Recompute fda_assets.next_catalyst_date from earliest future extracted_facts '
  'pdufa_date and catalyst_universe FDA-typed rows. Never regresses an existing '
  'sooner date — only fills NULL or moves to an earlier date.';

-- ---------------------------------------------------------------------------
-- 2) extracted_facts trigger — fires only on PDUFA date facts.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_assets_next_catalyst_from_extracted_facts()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  IF NEW.asset_id IS NOT NULL THEN
    PERFORM public.fda_assets_recompute_next_catalyst_date(NEW.asset_id);
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fda_assets_next_catalyst_facts_tg
  ON public.extracted_facts;
CREATE TRIGGER fda_assets_next_catalyst_facts_tg
  AFTER INSERT OR UPDATE OF fact_text, fact_type, asset_id
  ON public.extracted_facts
  FOR EACH ROW
  WHEN (NEW.fact_type = 'pdufa_date' AND NEW.asset_id IS NOT NULL)
  EXECUTE FUNCTION public.fda_assets_next_catalyst_from_extracted_facts();

-- ---------------------------------------------------------------------------
-- 3) catalyst_universe trigger — fans out to every asset owned by the entity.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_assets_next_catalyst_from_catalyst_universe()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_asset_id uuid;
BEGIN
  IF NEW.entity_id IS NULL THEN
    RETURN NEW;
  END IF;
  FOR v_asset_id IN
    SELECT id FROM public.fda_assets WHERE entity_id = NEW.entity_id
  LOOP
    PERFORM public.fda_assets_recompute_next_catalyst_date(v_asset_id);
  END LOOP;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fda_assets_next_catalyst_universe_tg
  ON public.catalyst_universe;
CREATE TRIGGER fda_assets_next_catalyst_universe_tg
  AFTER INSERT OR UPDATE OF catalyst_date, catalyst_type, entity_id
  ON public.catalyst_universe
  FOR EACH ROW
  WHEN (
    NEW.catalyst_type IN ('fda_approval','fda_crl','phase3_readout','adcomm')
    AND NEW.entity_id IS NOT NULL
  )
  EXECUTE FUNCTION public.fda_assets_next_catalyst_from_catalyst_universe();

-- ---------------------------------------------------------------------------
-- 4) Backfill helper. Loops every active fda_asset; safe to re-run.
-- Returns (rows_seen, rows_updated).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_assets_backfill_next_catalyst_date()
RETURNS TABLE (rows_seen int, rows_updated int)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_id uuid;
  v_before date;
  v_after date;
  v_seen int := 0;
  v_updated int := 0;
BEGIN
  FOR v_id, v_before IN
    SELECT id, next_catalyst_date FROM public.fda_assets WHERE is_active = true
  LOOP
    v_seen := v_seen + 1;
    PERFORM public.fda_assets_recompute_next_catalyst_date(v_id);
    SELECT next_catalyst_date INTO v_after FROM public.fda_assets WHERE id = v_id;
    IF v_after IS DISTINCT FROM v_before THEN
      v_updated := v_updated + 1;
    END IF;
  END LOOP;
  RETURN QUERY SELECT v_seen, v_updated;
END;
$$;

COMMENT ON FUNCTION public.fda_assets_backfill_next_catalyst_date() IS
  'Replay fda_assets_recompute_next_catalyst_date over every active fda_asset. '
  'Run once after migration to populate the column from existing extracted_facts '
  'and catalyst_universe rows; the triggers keep it fresh thereafter.';
