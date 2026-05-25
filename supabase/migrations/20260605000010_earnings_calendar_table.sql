-- =============================================================================
-- Phase 3a — earnings_calendar table
--
-- Q1 confounder audit (WI-5) needs to know when each FDA event coincided with
-- a same-ticker earnings announcement ±5 trading days. yfinance and Polygon
-- both expose earnings dates; we keep multi-source rows side-by-side
-- (UNIQUE on ticker + date + source) so the audit reader can prefer the
-- highest-confidence source per (ticker, earnings_date).
--
-- Backfill: 5y per tradeable ticker via phase3a_backfill_earnings_calendar.py.
-- Daily refresh: pg_cron job earnings-calendar-daily (06:10 UTC), separate
-- migration. Today−7d to today+90d sliding window.
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (Phase 3a calendars)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.earnings_calendar (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker          text NOT NULL,
  earnings_date   date NOT NULL,
  session         text CHECK (session IN ('bmo','amc','during','unknown')) DEFAULT 'unknown',
  fiscal_period   text,
  is_estimated    boolean NOT NULL DEFAULT true,
  source          text NOT NULL CHECK (source IN ('yfinance','polygon','manual')),
  confidence      numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
  fetched_at      timestamptz NOT NULL DEFAULT now(),
  raw_payload     jsonb,
  UNIQUE (ticker, earnings_date, source)
);

CREATE INDEX IF NOT EXISTS earnings_calendar_ticker_date_idx
  ON public.earnings_calendar (ticker, earnings_date DESC);
CREATE INDEX IF NOT EXISTS earnings_calendar_date_idx
  ON public.earnings_calendar (earnings_date);

COMMENT ON TABLE public.earnings_calendar IS
  'Earnings announcement dates per ticker, fed by daily yfinance + Polygon fetchers. Q1 audit reads this to flag FDA events that landed ±5 trading days of an earnings announcement (confounder). Multi-source rows coexist; readers resolve via ORDER BY confidence DESC, fetched_at DESC.';

COMMENT ON COLUMN public.earnings_calendar.is_estimated IS
  'True = forward-looking estimated date (e.g. yfinance Ticker.calendar). False = confirmed announce date (post-earnings).';

COMMENT ON COLUMN public.earnings_calendar.session IS
  'bmo = before market open, amc = after market close, during = intraday, unknown = unspecified. Confounder window math uses trading days centered on this date — the session distinguishes "this calendar day" from "the next trading day after the print".';
