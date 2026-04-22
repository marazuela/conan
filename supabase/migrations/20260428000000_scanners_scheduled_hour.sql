-- Conan v2 — per-scanner release-time scheduling
--
-- Context:
--   dispatch_daily used to fire every operational `daily` scanner at a single
--   09:00 UTC tick. That ignored when each source actually publishes — LSE
--   RNS releases cluster 06:00 UTC, congressional STOCK Act filings drop
--   during US trading hours, SEC enforcement press releases often land
--   post-close. This migration adds scheduled_hour_utc so each scanner fires
--   in the UTC hour closest to its source's release window.
--
--   Runtime: dispatch_release_times (replacing dispatch_daily) fires at
--   06/08/13/17/21 UTC and queries the registry for scanners matching the
--   current hour. See modal_workers/app.py::_dispatch_by_hour.
--
--   NULL = legacy default — falls through to the 13 UTC bucket so unset rows
--   keep firing once a day if someone adds a scanner without picking a slot.

ALTER TABLE public.scanners
  ADD COLUMN IF NOT EXISTS scheduled_hour_utc smallint
    CHECK (scheduled_hour_utc IS NULL OR scheduled_hour_utc BETWEEN 0 AND 23);

COMMENT ON COLUMN public.scanners.scheduled_hour_utc IS
  'Hour of day (UTC) when this daily scanner fires. NULL routes to the 13 UTC default bucket. Only consulted when cadence=''daily''.';

-- Reclassify four 3h scanners to daily. fda_pdufa_pipeline emits <=1 signal
-- per run on most days, making 8x-daily cadence pure overhead. The three
-- international scanners (LSE/ASX/TDNET) publish at specific market-close
-- windows so a once-daily post-close sweep captures the full day's feed.
UPDATE public.scanners SET cadence = 'daily' WHERE name = 'fda_pdufa_pipeline';
UPDATE public.scanners SET cadence = 'daily' WHERE name = 'lse_rns_scanner';
UPDATE public.scanners SET cadence = 'daily' WHERE name = 'asx_scanner';
UPDATE public.scanners SET cadence = 'daily' WHERE name = 'tdnet_scanner';

-- Release-time assignments. Set for paused scanners too so the schedule is
-- correct the moment they flip to operational.
--
-- 06 UTC — EU pre-open
UPDATE public.scanners SET scheduled_hour_utc = 6  WHERE name = 'lse_rns_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 6  WHERE name = 'esma_short_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 6  WHERE name = 'bse_nse_scanner';

-- 08 UTC — APAC post-close
UPDATE public.scanners SET scheduled_hour_utc = 8  WHERE name = 'asx_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 8  WHERE name = 'tdnet_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 8  WHERE name = 'hkex_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 8  WHERE name = 'kind_scanner';

-- 13 UTC — US pre-open / Americas morning
UPDATE public.scanners SET scheduled_hour_utc = 13 WHERE name = 'fda_pdufa_pipeline';
UPDATE public.scanners SET scheduled_hour_utc = 13 WHERE name = 'cvm_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 13 WHERE name = 'sedar_plus_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 13 WHERE name = 'bmv_scanner';

-- 17 UTC — US midday (STOCK Act filings land during US trading hours)
UPDATE public.scanners SET scheduled_hour_utc = 17 WHERE name = 'congressional_trading';

-- 21 UTC — US post-close
UPDATE public.scanners SET scheduled_hour_utc = 21 WHERE name = 'sec_enforcement_scanner';
UPDATE public.scanners SET scheduled_hour_utc = 21 WHERE name = 'courtlistener_scanner';
