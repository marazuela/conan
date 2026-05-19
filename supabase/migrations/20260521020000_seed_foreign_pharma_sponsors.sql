-- 2026-05-11 — Phase 2A: foreign pharma sponsor → ticker aliases (R4)
--
-- Follow-up to 20260430010000_seed_binary_catalyst_sponsor_tickers.sql.
-- Names sourced from the first day of unresolved_sponsor_log telemetry
-- (R4 Phase 1, migration 20260521010000). Includes only entries with
-- high-confidence ticker mappings — uncertain ones (Sichuan Baili,
-- Suzhou Zelgen, etc.) deliberately omitted; revisit after 1-2 weeks of
-- log data + per-name verification via OpenFIGI /v3/mapping.
--
-- Out of scope: private sponsors (Boehringer Ingelheim, Beacon
-- Therapeutics, Biocad, Cerevance). The seed migration UPDATE pattern
-- requires a public ticker; private companies are tracked but not
-- backfilled with one.
--
-- Idempotent: only updates rows where primary_ticker IS NULL.

WITH sponsor_map(name, ticker, mic, country) AS (
    VALUES
        -- Greater China primary listings
        ('Akeso',                                  '9926',   'XHKG', 'HK'),
        ('Jiangsu HengRui Medicine Co., Ltd.',     '600276', 'XSHG', 'CN'),
        ('Gan & Lee Pharmaceuticals.',             '603087', 'XSHG', 'CN'),

        -- Korea primary listings
        ('AriBio Co., Ltd.',                       '140860', 'XKRX', 'KR'),
        ('Celltrion',                              '068270', 'XKRX', 'KR'),

        -- Europe primary listings
        ('Camurus AB',                             'CAMX',   'XSTO', 'SE'),

        -- US-listed names that the SEC tickers cache occasionally misses
        -- (recent IPOs / name changes). Belt-and-suspenders so signal
        -- emission is robust to the company_tickers.json refresh cycle.
        ('Avidity Biosciences, Inc.',              'RNA',    'XNAS', 'US'),
        ('EyePoint Pharmaceuticals, Inc.',         'EYPT',   'XNAS', 'US')
)
UPDATE entities AS e
SET primary_ticker = m.ticker,
    primary_mic    = m.mic,
    country        = m.country
FROM sponsor_map AS m
WHERE e.name = m.name
  AND e.primary_ticker IS NULL;

-- Register ticker_mic identifiers so future scanner emissions with a
-- (ticker, mic) hint resolve to these existing entities rather than
-- creating duplicates.
INSERT INTO entity_identifiers (entity_id, id_type, id_value, priority)
SELECT e.id, 'ticker_mic', e.primary_ticker || '@' || e.primary_mic, 20
FROM entities e
WHERE e.name IN (
        'Akeso',
        'Jiangsu HengRui Medicine Co., Ltd.',
        'Gan & Lee Pharmaceuticals.',
        'AriBio Co., Ltd.',
        'Celltrion',
        'Camurus AB',
        'Avidity Biosciences, Inc.',
        'EyePoint Pharmaceuticals, Inc.'
    )
  AND e.primary_ticker IS NOT NULL
  AND e.primary_mic    IS NOT NULL
ON CONFLICT (id_type, id_value) DO NOTHING;
