-- Smoke test for the 20260532000000 resurrection-guard redefinition of
-- public._past_catalyst_signal_drain.
--
-- Coverage (the two behaviors added on top of 20260514120000):
--   1. A past-catalyst pdufa_approaching row (immediate) is drained — proves
--      the new signal_type is in scope and resolves via the pdufa_date path.
--   2. A drained row whose band_with_bonus was NULL gets band_with_bonus =
--      'archive' (NOT left NULL) — the resurrection guard vs
--      orphan_convergence_sweeper's `band_with_bonus IS NULL` filter.
--   3. A future-catalyst pdufa_approaching row is NOT drained.
--
-- Run with:
--   supabase db execute --file supabase/tests/past_catalyst_drain_resurrection_guard_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end, so the
-- target database is unaffected. Failed assertions RAISE EXCEPTION.

BEGIN;

DO $$
DECLARE
  v_rubric_id   uuid;
  v_today       date := current_date;
  v_past_pdufa  text := to_char(v_today - interval '6 days', 'YYYY-MM-DD');
  v_future_pdufa text := to_char(v_today + interval '45 days', 'YYYY-MM-DD');
  v_band        signal_band;
  v_band_wb     signal_band;
  v_reason      text;
BEGIN
  SELECT id INTO v_rubric_id FROM public.rubrics LIMIT 1;
  IF v_rubric_id IS NULL THEN
    RAISE EXCEPTION 'smoke: no rubrics row to satisfy signals.rubric_version_id FK';
  END IF;

  -- (1) past pdufa_approaching, immediate, band_with_bonus NULL (orphan
  --     pattern) — must be drained AND band_with_bonus forced to archive.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-rezz-pdufa-approaching-1', 'binary_catalyst', v_rubric_id,
    'smoke-rezz-hash-1', now(), now(),
    'pdufa_approaching', 30.0, 'immediate', NULL,
    jsonb_build_object('pdufa_date', v_past_pdufa, 'ticker', 'TST'),
    '{}'::jsonb
  );

  -- (2) future pdufa_approaching — must NOT be drained.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-rezz-pdufa-approaching-future-1', 'binary_catalyst', v_rubric_id,
    'smoke-rezz-hash-2', now(), now(),
    'pdufa_approaching', 35.0, 'watchlist', NULL,
    jsonb_build_object('pdufa_date', v_future_pdufa, 'ticker', 'TST'),
    '{}'::jsonb
  );

  PERFORM public._past_catalyst_signal_drain(2, 1000);

  -- Assert (1): drained, band & band_with_bonus both archive.
  SELECT band, band_with_bonus, dimensions->>'_drain_reason'
    INTO v_band, v_band_wb, v_reason
    FROM public.signals WHERE signal_id = 'smoke-rezz-pdufa-approaching-1';
  IF v_band IS DISTINCT FROM 'archive'::signal_band THEN
    RAISE EXCEPTION 'smoke: pdufa_approaching past row not archived (band=%)', v_band;
  END IF;
  IF v_band_wb IS DISTINCT FROM 'archive'::signal_band THEN
    RAISE EXCEPTION 'smoke: resurrection guard failed — band_with_bonus=% (expected archive, was NULL pre-drain)', v_band_wb;
  END IF;
  IF v_reason IS DISTINCT FROM 'past_catalyst_no_resolution' THEN
    RAISE EXCEPTION 'smoke: _drain_reason not stamped (got %)', v_reason;
  END IF;

  -- Assert (2): future row untouched.
  SELECT band INTO v_band
    FROM public.signals WHERE signal_id = 'smoke-rezz-pdufa-approaching-future-1';
  IF v_band IS DISTINCT FROM 'watchlist'::signal_band THEN
    RAISE EXCEPTION 'smoke: future pdufa_approaching wrongly drained (band=%)', v_band;
  END IF;

  RAISE NOTICE 'past_catalyst_drain_resurrection_guard_smoke: ALL ASSERTIONS PASSED';
END $$;

ROLLBACK;
