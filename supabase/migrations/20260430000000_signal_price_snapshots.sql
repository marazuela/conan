-- 20260430000000 — signal_price_snapshots
--
-- Daily price-tracking time series for signals + candidates. Producer for the
-- `outcomes.realized_move_{1d,7d,30d}` columns (added in 20260424000000) and
-- the `accuracy_metrics.timing_auditor.mean_realized_move_*` aggregates
-- (20260425000000). Until now those columns had no writer.
--
-- One row per (subject, horizon_days). Subject is a signal OR a candidate
-- (XOR via CHECK). Tracks both because:
--   - candidates: feed outcomes / precision_auditor (the curated set)
--   - watchlist/immediate signals: feed challenger_retro ("would-we-have-caught-it")
--
-- t=0 anchor is the subject's created_at::date. signed_move_pct is already
-- direction-flipped (long: raw, short: -raw) so positive = thesis was right;
-- `outcomes.realized_move_*` gets the same signed value so consumers don't
-- have to remember to flip.
--
-- Producer: modal_workers/evaluators/price_tracker.py (Modal scheduled fn
-- evaluate_ticker_movement, daily 23:30 UTC ≈ 18:30 ET, post-US-close).

CREATE TABLE IF NOT EXISTS public.signal_price_snapshots (
  id                BIGSERIAL PRIMARY KEY,
  signal_id         text REFERENCES public.signals(signal_id) ON DELETE CASCADE,
  candidate_id      uuid REFERENCES public.candidates(id)     ON DELETE CASCADE,
  ticker            text NOT NULL,
  mic               text,
  thesis_direction  text NOT NULL CHECK (thesis_direction IN ('long','short','neutral')),
  anchor_date       date NOT NULL,
  horizon_days      smallint NOT NULL CHECK (horizon_days IN (1,7,30)),
  anchor_close      numeric(18,6),
  horizon_close     numeric(18,6),
  raw_move_pct      numeric(8,4),
  signed_move_pct   numeric(8,4),
  fetch_status      text NOT NULL CHECK (fetch_status IN (
                      'ok','no_data','stale_anchor','pending','neutral_skipped'
                    )),
  captured_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT signal_price_snapshots_subject_present
    CHECK (signal_id IS NOT NULL OR candidate_id IS NOT NULL)
);

-- One row per (subject, horizon). NULL-distinct semantics in Postgres mean we
-- need partial unique indexes, one per subject column, to make UPSERT safe.
CREATE UNIQUE INDEX IF NOT EXISTS signal_price_snapshots_signal_horizon_uniq
  ON public.signal_price_snapshots (signal_id, horizon_days)
  WHERE signal_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS signal_price_snapshots_candidate_horizon_uniq
  ON public.signal_price_snapshots (candidate_id, horizon_days)
  WHERE candidate_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS signal_price_snapshots_anchor_idx
  ON public.signal_price_snapshots (anchor_date, horizon_days, fetch_status);

CREATE INDEX IF NOT EXISTS signal_price_snapshots_candidate_idx
  ON public.signal_price_snapshots (candidate_id)
  WHERE candidate_id IS NOT NULL;

ALTER TABLE public.signal_price_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY signal_price_snapshots_select
  ON public.signal_price_snapshots FOR SELECT TO authenticated USING (true);

-- service_role bypasses RLS; no INSERT/UPDATE policy is granted to authenticated
-- because only the Modal worker should write here.

COMMENT ON TABLE public.signal_price_snapshots IS
  'Daily ticker price tracking, one row per (subject, horizon_days). Subject is '
  'a signal or candidate. signed_move_pct is direction-flipped so positive = '
  'thesis was right. Written daily by modal_workers price_tracker.';

COMMENT ON COLUMN public.signal_price_snapshots.signed_move_pct IS
  'raw_move_pct sign-flipped for short direction (long: raw; short: -raw; '
  'neutral: NULL with fetch_status=neutral_skipped). Mirrored into '
  'outcomes.realized_move_{1d,7d,30d} when candidate_id is set.';

COMMENT ON COLUMN public.signal_price_snapshots.anchor_date IS
  'Subject created_at::date in UTC. t=0 for the horizon math.';

COMMENT ON COLUMN public.signal_price_snapshots.fetch_status IS
  'ok            = both closes fetched, signed_move_pct populated. '
  'no_data       = yfinance returned nothing for ticker on either date. '
  'stale_anchor  = anchor_close found but horizon_close not yet available. '
  'pending       = horizon hasn''t elapsed yet (placeholder, generally not written). '
  'neutral_skipped = thesis_direction=neutral; no signed comparison performed.';
