-- Smoke test for public._past_catalyst_signal_drain.
--
-- Coverage:
--   1. A past-catalyst pre_phase3_readout (PCD=today-5) is drained: band &
--      band_with_bonus → archive, dimensions stamped with _drain_reason etc.
--   2. A future-catalyst pre_phase3_readout (PCD=today+30) is NOT drained.
--   3. A past-catalyst row already carrying _drain_reason is NOT re-stamped
--      (idempotency).
--   4. A past-catalyst fda_decision (pdufa_date=today-7) is drained — covers
--      the pdufa_date code path.
--   5. A past-catalyst row at band='archive' is ignored — we only touch
--      immediate/watchlist.
--   6. _drained_by carries the cron source string.
--
-- Run with:
--   supabase db execute --file supabase/tests/past_catalyst_signal_drain_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end, so the
-- target database is unaffected. Failed assertions RAISE EXCEPTION.

BEGIN;

DO $$
DECLARE
  v_rubric_id     uuid;
  v_today         date := current_date;
  v_past_pcd      text := to_char(v_today - interval '5 days', 'YYYY-MM-DD');
  v_future_pcd    text := to_char(v_today + interval '30 days', 'YYYY-MM-DD');
  v_past_pdufa    text := to_char(v_today - interval '7 days', 'YYYY-MM-DD');
  v_drain_result  jsonb;
  v_band          signal_band;
  v_band_wb       signal_band;
  v_dims          jsonb;
  v_reason        text;
  v_drained_by    text;
  v_existing_dims jsonb;
BEGIN
  -- Pick any rubric for FK satisfaction; the drain function does not care
  -- about rubric content, only signal_type / band / raw_payload / dimensions.
  SELECT id INTO v_rubric_id FROM public.rubrics LIMIT 1;
  IF v_rubric_id IS NULL THEN
    RAISE EXCEPTION 'smoke: no rubrics row to satisfy signals.rubric_version_id FK';
  END IF;

  -- ---- Fixture rows ------------------------------------------------------

  -- (1) past PCD, immediate, undrained — should be drained.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-drain-past-1', 'binary_catalyst', v_rubric_id,
    'smoke-hash-1', now(), now(),
    'pre_phase3_readout', 30.0, 'immediate', 'immediate',
    jsonb_build_object('primary_completion_date', v_past_pcd, 'ticker', 'TST'),
    '{}'::jsonb
  );

  -- (2) future PCD, watchlist — should NOT be drained.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-drain-future-1', 'binary_catalyst', v_rubric_id,
    'smoke-hash-2', now(), now(),
    'pre_phase3_readout', 35.0, 'watchlist', 'watchlist',
    jsonb_build_object('primary_completion_date', v_future_pcd, 'ticker', 'TST'),
    '{}'::jsonb
  );

  -- (3) past PCD, watchlist, BUT already drained — must remain untouched.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-drain-already-1', 'binary_catalyst', v_rubric_id,
    'smoke-hash-3', now(), now(),
    'pre_phase3_readout', 25.0, 'watchlist', 'watchlist',
    jsonb_build_object('primary_completion_date', v_past_pcd, 'ticker', 'TST'),
    jsonb_build_object(
      '_drain_reason', 'past_catalyst_no_resolution',
      '_drained_at',   (now() - interval '1 day'),
      '_drained_by',   'manual_pipeline_fix_D-PIPELINE-FIX-2026-05-13',
      '_drain_note',   'pre-existing'
    )
  );

  -- (4) past PDUFA, watchlist fda_decision — should be drained via pdufa_date path.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-drain-fda-decision-1', 'binary_catalyst', v_rubric_id,
    'smoke-hash-4', now(), now(),
    'fda_decision', 30.6, 'watchlist', 'watchlist',
    jsonb_build_object('pdufa_date', v_past_pdufa, 'ticker', 'ARGX'),
    '{}'::jsonb
  );

  -- (5) past PCD but ALREADY band=archive — drain must ignore.
  INSERT INTO public.signals (
    signal_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date,
    signal_type, score, band, band_with_bonus,
    raw_payload, dimensions
  ) VALUES (
    'smoke-drain-archive-1', 'binary_catalyst', v_rubric_id,
    'smoke-hash-5', now(), now(),
    'pre_phase3_readout', 15.0, 'archive', 'archive',
    jsonb_build_object('primary_completion_date', v_past_pcd, 'ticker', 'ARC'),
    '{}'::jsonb
  );

  -- Capture (3)'s dimensions for the idempotency check.
  SELECT dimensions INTO v_existing_dims
    FROM public.signals WHERE signal_id = 'smoke-drain-already-1';

  -- ---- Exercise: run the drain with N=2 ----------------------------------
  v_drain_result := public._past_catalyst_signal_drain(2, 100);

  -- ---- Assert 1: past-PCD pre_phase3 row drained -------------------------
  SELECT band, band_with_bonus, dimensions
    INTO v_band, v_band_wb, v_dims
    FROM public.signals WHERE signal_id = 'smoke-drain-past-1';
  IF v_band <> 'archive' OR v_band_wb <> 'archive' THEN
    RAISE EXCEPTION 'smoke: past-PCD row expected band=archive/archive, got %/%',
                    v_band, v_band_wb;
  END IF;
  v_reason     := v_dims->>'_drain_reason';
  v_drained_by := v_dims->>'_drained_by';
  IF v_reason <> 'past_catalyst_no_resolution' THEN
    RAISE EXCEPTION 'smoke: past-PCD row expected _drain_reason=past_catalyst_no_resolution, got %', v_reason;
  END IF;
  IF v_drained_by <> 'cron:past_catalyst_signal_drain' THEN
    RAISE EXCEPTION 'smoke: past-PCD row expected _drained_by=cron:past_catalyst_signal_drain, got %', v_drained_by;
  END IF;
  IF NOT (v_dims ? '_drained_at') OR NOT (v_dims ? '_drain_note') THEN
    RAISE EXCEPTION 'smoke: past-PCD row missing _drained_at / _drain_note keys';
  END IF;

  -- ---- Assert 2: future-PCD row untouched --------------------------------
  SELECT band, band_with_bonus, dimensions
    INTO v_band, v_band_wb, v_dims
    FROM public.signals WHERE signal_id = 'smoke-drain-future-1';
  IF v_band <> 'watchlist' OR v_band_wb <> 'watchlist' THEN
    RAISE EXCEPTION 'smoke: future-PCD row expected band=watchlist/watchlist, got %/%',
                    v_band, v_band_wb;
  END IF;
  IF v_dims ? '_drain_reason' THEN
    RAISE EXCEPTION 'smoke: future-PCD row should not have _drain_reason';
  END IF;

  -- ---- Assert 3: already-drained row unchanged (idempotency) -------------
  SELECT dimensions INTO v_dims
    FROM public.signals WHERE signal_id = 'smoke-drain-already-1';
  IF v_dims <> v_existing_dims THEN
    RAISE EXCEPTION 'smoke: already-drained row was re-stamped, before=% after=%',
                    v_existing_dims, v_dims;
  END IF;

  -- ---- Assert 4: fda_decision (pdufa_date path) drained -------------------
  SELECT band, dimensions->>'_drain_reason'
    INTO v_band, v_reason
    FROM public.signals WHERE signal_id = 'smoke-drain-fda-decision-1';
  IF v_band <> 'archive' OR v_reason <> 'past_catalyst_no_resolution' THEN
    RAISE EXCEPTION 'smoke: fda_decision row expected drained, got band=% reason=%',
                    v_band, v_reason;
  END IF;

  -- ---- Assert 5: pre-existing archive row not touched --------------------
  SELECT band, dimensions
    INTO v_band, v_dims
    FROM public.signals WHERE signal_id = 'smoke-drain-archive-1';
  IF v_band <> 'archive' THEN
    RAISE EXCEPTION 'smoke: pre-archived row band changed unexpectedly to %', v_band;
  END IF;
  IF v_dims ? '_drain_reason' THEN
    RAISE EXCEPTION 'smoke: pre-archived row should not gain _drain_reason';
  END IF;

  -- ---- Assert 6: return value reports the right counts -------------------
  IF (v_drain_result->>'drained')::int <> 2 THEN
    RAISE EXCEPTION 'smoke: expected drained=2 (smoke-drain-past-1 + smoke-drain-fda-decision-1), got %',
                    v_drain_result->>'drained';
  END IF;
  IF ((v_drain_result->'by_signal_type'->>'pre_phase3_readout')::int) <> 1 THEN
    RAISE EXCEPTION 'smoke: expected by_signal_type.pre_phase3_readout=1, got %',
                    v_drain_result->'by_signal_type'->>'pre_phase3_readout';
  END IF;
  IF ((v_drain_result->'by_signal_type'->>'fda_decision')::int) <> 1 THEN
    RAISE EXCEPTION 'smoke: expected by_signal_type.fda_decision=1, got %',
                    v_drain_result->'by_signal_type'->>'fda_decision';
  END IF;

  -- ---- Assert 7: second invocation drains nothing (idempotency at fn level)
  v_drain_result := public._past_catalyst_signal_drain(2, 100);
  IF (v_drain_result->>'drained')::int <> 0 THEN
    RAISE EXCEPTION 'smoke: second invocation expected drained=0, got %',
                    v_drain_result->>'drained';
  END IF;

  RAISE NOTICE 'smoke: all assertions passed';
END;
$$;

ROLLBACK;
