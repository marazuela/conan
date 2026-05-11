-- F-300 fix: continuous-discovery for fda_regulatory_events.
--
-- Background
-- ----------
-- Before this migration the only writer to fda_regulatory_events was the
-- manual one-shot `modal_workers/scripts/fda_backfill_watchlist.py`. It ran
-- once on 2026-05-04 and populated 35 rows. After that, the table stayed
-- frozen — fda_signal_bridge kept recycling the same 32 events every 3h, the
-- enqueue_fda_agent_reviews trigger had nothing to fire on, and the entire
-- v3 agentic pipeline (specialist reviews → IC memo → promote-to-thesis)
-- was starved of input.
--
-- Meanwhile, fda_pdufa_pipeline emits ~1.4 binary_catalyst signals/day
-- (pdufa_*, fda_decision, eop2_meeting types) and pre_phase3_readout_scanner
-- emits pre_phase3_readout signals. These two scanners ARE doing continuous
-- FDA discovery — they just write to `signals`, not `fda_regulatory_events`.
--
-- This migration bridges the two pipelines: any high-confidence binary_catalyst
-- signal automatically materializes a corresponding fda_regulatory_events row,
-- which then triggers the existing enqueue_fda_agent_reviews fan-out.
--
-- Design
-- ------
-- AFTER INSERT trigger on public.signals. Gates:
--   • scoring_profile = 'binary_catalyst'
--   • entity_id IS NOT NULL  (downstream agents need a ticker)
--   • score_with_bonus >= 25 OR score >= 25  (skip very-low-conviction noise)
--   • signal_type in the allowlist below
--
-- Per-signal-type mapping (event_type values constrained by
-- fda_regulatory_events_event_type_check: pdufa, adcom, phase3_readout, eop2,
-- approval, crl, presumed_crl, date_change, withdrawal):
--
--   pdufa_imminent, pdufa_approaching, pdufa_watchlist
--     → event_type='pdufa', event_status='pending'
--     hash: sha256('pdufa|' || ticker || '|' || drug || '|' || pdufa_date)
--     Stable across the imminent/approaching/watchlist subtype changes so
--     the same physical PDUFA produces exactly one event row, not three.
--
--   pdufa_date_advanced, pdufa_date_delayed
--     → event_type='date_change', event_status='pending'
--     hash: sha256('date_change|' || ticker || '|' || drug || '|' ||
--                  pdufa_date || '|' || previous_pdufa_date)
--     One event per unique (new_date, previous_date) pair — captures the
--     specific date move, not just "PDUFA exists".
--
--   fda_decision (raw_payload.status ∈ {approved, crl, presumed_crl, resolved_crl})
--     → event_type ∈ {approval, crl, presumed_crl}, event_status='resolved'
--     hash: sha256('decision|' || ticker || '|' || drug || '|' || status || '|' || pdufa_date)
--     event_date = crl_date if present else pdufa_date.
--     The enqueue_fda_agent_reviews trigger skips resolution event_types by
--     design, so these flow into the table but don't generate agent reviews.
--
--   eop2_meeting
--     → event_type='eop2', event_status='pending'
--     hash: sha256('eop2|' || adsh)  — adsh is already the dedup key on signals side
--     event_date = file_date (8-K filing date)
--
--   pre_phase3_readout
--     → event_type='phase3_readout', event_status='pending'
--     hash: sha256('phase3_readout|' || ticker || '|' || drug || '|' || primary_completion_date)
--     event_date = primary_completion_date.
--     Reuses the auto_seed_fda_asset hint blob structure from migration
--     20260519000000 — the asset is also created (or already exists) via
--     that older trigger before this one fires.
--
-- Asset resolution
-- ----------------
-- Find or create the fda_assets row via UPSERT on UNIQUE (ticker, drug_name,
-- application_number). application_number is taken from raw_payload when
-- available (PDUFA signals carry it), else '' (matches the pre_phase3
-- auto-seed convention). If the asset is new, populate program_status from
-- the signal_type:
--   pre_phase3_readout → 'phase3'
--   pdufa_* / fda_decision / date_change → 'filed' (NDA submitted, awaiting decision)
--   eop2_meeting → 'phase2' (EOP2 is at the Phase-2/3 boundary)
--
-- Idempotency
-- -----------
-- fda_regulatory_events UNIQUE (asset_id, event_type, event_date,
-- source_content_hash). With stable hashes per physical event, re-emission
-- of the same signal_type is an UPSERT no-op. Subtype changes
-- (watchlist→approaching→imminent) all hash to the same pdufa event row.
--
-- Rollback
--   DROP TRIGGER IF EXISTS fda_regulatory_event_from_signal_tg ON public.signals;
--   DROP FUNCTION IF EXISTS public.fda_regulatory_event_from_signal();

CREATE OR REPLACE FUNCTION public.fda_regulatory_event_from_signal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_signal_type        text;
  v_ticker             text;
  v_drug_name          text;
  v_app_num            text;
  v_indication         text;
  v_sponsor            text;
  v_pdufa_date_str     text;
  v_previous_pdufa     text;
  v_pcd                text;   -- primary_completion_date for pre_phase3
  v_file_date          text;   -- 8-K filing date for eop2
  v_adsh               text;
  v_status             text;   -- raw_payload.status for fda_decision
  v_crl_date           text;
  v_score              numeric;
  v_event_type         text;
  v_event_status       text;
  v_event_date         date;
  v_hash               text;
  v_asset_id           uuid;
  v_program_status     text;
  v_hint               jsonb;
BEGIN
  -- Gate 1: profile + entity
  IF NEW.scoring_profile IS DISTINCT FROM 'binary_catalyst' THEN
    RETURN NEW;
  END IF;
  IF NEW.entity_id IS NULL THEN
    RETURN NEW;
  END IF;

  -- Gate 2: conviction floor. Use score_with_bonus when set (post-convergence),
  -- else the raw score (pre-convergence). 25 = roughly the band='watchlist'
  -- threshold; below that, the v3 review queue isn't worth burning.
  v_score := COALESCE(NEW.score_with_bonus, NEW.score, 0);
  IF v_score < 25 THEN
    RETURN NEW;
  END IF;

  v_signal_type := NEW.signal_type;

  -- Gate 3: only handle FDA-shaped signal types
  IF v_signal_type NOT IN (
    'pdufa_imminent','pdufa_approaching','pdufa_watchlist',
    'pdufa_date_advanced','pdufa_date_delayed',
    'fda_decision','pre_phase3_readout','eop2_meeting'
  ) THEN
    RETURN NEW;
  END IF;

  -- Dispatch by signal_type. Each branch sets:
  --   v_ticker, v_drug_name, v_app_num,
  --   v_event_type, v_event_status, v_event_date,
  --   v_hash, v_program_status
  -- and any indication/sponsor/* fields needed for asset creation.

  IF v_signal_type IN ('pdufa_imminent','pdufa_approaching','pdufa_watchlist') THEN
    v_ticker         := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name      := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num        := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication     := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor        := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_event_type     := 'pdufa';
    v_event_status   := 'pending';
    BEGIN v_event_date := v_pdufa_date_str::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash           := 'sha256:' || encode(extensions.digest(
      'pdufa|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' || COALESCE(v_pdufa_date_str,''),
      'sha256'), 'hex');
    v_program_status := 'filed';

  ELSIF v_signal_type IN ('pdufa_date_advanced','pdufa_date_delayed') THEN
    v_ticker         := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name      := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num        := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication     := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor        := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_previous_pdufa := NULLIF(NEW.raw_payload->>'previous_pdufa_date','');
    v_event_type     := 'date_change';
    v_event_status   := 'pending';
    BEGIN v_event_date := v_pdufa_date_str::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash           := 'sha256:' || encode(extensions.digest(
      'date_change|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' ||
      COALESCE(v_pdufa_date_str,'') || '|' || COALESCE(v_previous_pdufa,''),
      'sha256'), 'hex');
    v_program_status := 'filed';

  ELSIF v_signal_type = 'fda_decision' THEN
    v_ticker         := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name      := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num        := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication     := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor        := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_crl_date       := NULLIF(NEW.raw_payload->>'crl_date','');
    v_status         := COALESCE(NULLIF(NEW.raw_payload->>'status',''), '');
    -- Map raw_payload.status → event_type from the constrained set
    v_event_type := CASE
      WHEN v_status IN ('approved','approval')                  THEN 'approval'
      WHEN v_status = 'crl'                                     THEN 'crl'
      WHEN v_status IN ('presumed_crl','resolved_crl')          THEN 'presumed_crl'
      WHEN v_status = 'withdrawal'                              THEN 'withdrawal'
      ELSE NULL
    END;
    IF v_event_type IS NULL THEN
      RETURN NEW;  -- unrecognized status; don't write a malformed event
    END IF;
    v_event_status := 'resolved';
    BEGIN v_event_date := COALESCE(v_crl_date, v_pdufa_date_str)::date;
    EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'decision|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' ||
      v_event_type || '|' || COALESCE(v_pdufa_date_str,''),
      'sha256'), 'hex');
    v_program_status := CASE WHEN v_event_type = 'approval' THEN 'approved' ELSE 'filed' END;

  ELSIF v_signal_type = 'eop2_meeting' THEN
    v_ticker      := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name   := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num     := '';  -- no NDA at EOP2 stage
    v_sponsor     := NULLIF(NEW.raw_payload->>'company_name','');
    v_file_date   := NULLIF(NEW.raw_payload->>'file_date','');
    v_adsh        := NULLIF(NEW.raw_payload->>'adsh','');
    IF v_adsh IS NULL THEN
      RETURN NEW;  -- adsh is the dedup key; can't proceed without it
    END IF;
    v_event_type  := 'eop2';
    v_event_status := 'pending';
    BEGIN v_event_date := v_file_date::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest('eop2|' || v_adsh, 'sha256'), 'hex');
    v_program_status := 'phase2';

  ELSIF v_signal_type = 'pre_phase3_readout' THEN
    -- Mirrors auto_seed_fda_asset_from_pre_phase3 — the hint blob is set by
    -- pre_phase3_readout_scanner only when the sponsor resolves to a public
    -- issuer + has a usable drug name.
    v_hint := NEW.raw_payload->'auto_seed_fda_asset';
    IF v_hint IS NULL THEN
      RETURN NEW;
    END IF;
    v_ticker     := NULLIF(v_hint->>'ticker','');
    v_drug_name  := NULLIF(v_hint->>'drug_name','');
    v_app_num    := '';
    v_indication := NULLIF(v_hint->>'indication','');
    v_sponsor    := NULLIF(v_hint->>'sponsor_name','');
    v_pcd        := NULLIF(v_hint->>'primary_completion_date','');
    v_event_type := 'phase3_readout';
    v_event_status := 'pending';
    BEGIN v_event_date := v_pcd::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'phase3_readout|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' || COALESCE(v_pcd,''),
      'sha256'), 'hex');
    v_program_status := 'phase3';

  ELSE
    -- Defensive — shouldn't reach here given the earlier IN-list gate
    RETURN NEW;
  END IF;

  -- Required fields for asset creation
  IF v_ticker IS NULL OR v_drug_name IS NULL THEN
    RETURN NEW;
  END IF;

  -- Find or create fda_assets row. Use a SELECT-then-INSERT pattern to
  -- avoid the duplicate INSERT cost when most signals hit existing assets.
  SELECT id INTO v_asset_id
    FROM public.fda_assets
   WHERE ticker = v_ticker
     AND drug_name = v_drug_name
     AND application_number = v_app_num;

  IF v_asset_id IS NULL THEN
    INSERT INTO public.fda_assets (
      ticker, drug_name, application_number,
      entity_id, sponsor_name, indication, program_status,
      is_active, watch_priority,
      extensions
    )
    VALUES (
      v_ticker, v_drug_name, v_app_num,
      NEW.entity_id, v_sponsor, v_indication, v_program_status,
      true, 3,
      jsonb_build_object(
        'auto_seeded_from', 'fda_regulatory_event_from_signal',
        'seeding_signal_id', NEW.signal_id,
        'seeding_signal_type', v_signal_type,
        'seeded_at', now()
      )
    )
    ON CONFLICT (ticker, drug_name, application_number) DO NOTHING
    RETURNING id INTO v_asset_id;

    -- ON CONFLICT path: a concurrent insert grabbed the row. Re-select.
    IF v_asset_id IS NULL THEN
      SELECT id INTO v_asset_id
        FROM public.fda_assets
       WHERE ticker = v_ticker
         AND drug_name = v_drug_name
         AND application_number = v_app_num;
    END IF;
  END IF;

  IF v_asset_id IS NULL THEN
    RETURN NEW;  -- couldn't resolve asset; bail rather than NOT NULL violation
  END IF;

  -- INSERT fda_regulatory_events. Idempotent via UNIQUE constraint
  -- (asset_id, event_type, event_date, source_content_hash). Stable hashes
  -- mean re-emission of the same physical event is a no-op.
  INSERT INTO public.fda_regulatory_events (
    asset_id, event_type, event_date, event_status,
    source_content_hash, notes, extensions
  )
  VALUES (
    v_asset_id, v_event_type, v_event_date, v_event_status,
    v_hash,
    'auto-derived from signal: ' || v_signal_type || ' (signal_id=' || NEW.signal_id || ')',
    jsonb_build_object(
      'source_signal_id', NEW.signal_id,
      'source_signal_type', v_signal_type,
      'source_scoring_profile', NEW.scoring_profile,
      'source_score', v_score,
      'created_by', 'fda_regulatory_event_from_signal'
    )
  )
  ON CONFLICT (asset_id, event_type, event_date, source_content_hash) DO NOTHING;

  RETURN NEW;
END;
$func$;

DROP TRIGGER IF EXISTS fda_regulatory_event_from_signal_tg ON public.signals;
CREATE TRIGGER fda_regulatory_event_from_signal_tg
  AFTER INSERT ON public.signals
  FOR EACH ROW
  EXECUTE FUNCTION public.fda_regulatory_event_from_signal();

COMMENT ON FUNCTION public.fda_regulatory_event_from_signal() IS
  'F-300: maps high-confidence binary_catalyst signals (pdufa_*, fda_decision, '
  'eop2_meeting, pre_phase3_readout) to fda_regulatory_events rows so the v3 '
  'agentic pipeline (enqueue_fda_agent_reviews trigger → specialist reviews → '
  'IC memo → promote-to-thesis) gets continuous input. Idempotent via '
  'stable per-physical-event source_content_hash; re-emission is a no-op.';

-- pgcrypto is needed for `digest(text, 'sha256')`. Idempotent extension create.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
