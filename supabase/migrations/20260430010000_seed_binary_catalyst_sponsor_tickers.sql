-- Seed primary_ticker / primary_mic / country for entities created by
-- pre_phase3_readout_scanner. The scanner only emits a sponsor name (no ticker
-- hint), so resolve_or_create_entity creates entities with name only and the
-- dashboard renders them as "?". This backfill maps known industry sponsors
-- and their public-co subsidiaries to their listed tickers.
--
-- Idempotent: only updates rows where primary_ticker IS NULL.
-- Companion ticker_mic identifier rows are inserted with ON CONFLICT DO NOTHING
-- so future scanner runs that emit a (ticker, mic) hint match these entities
-- instead of creating duplicates.

WITH sponsor_map(name, ticker, mic, country) AS (
    VALUES
        -- US-listed pharma majors and biotechs
        ('AbbVie', 'ABBV', 'XNYS', 'US'),
        ('Amicus Therapeutics', 'FOLD', 'XNAS', 'US'),
        ('Arcus Biosciences, Inc.', 'RCUS', 'XNAS', 'US'),
        ('Arrowhead Pharmaceuticals', 'ARWR', 'XNAS', 'US'),
        ('Bristol-Myers Squibb', 'BMY', 'XNYS', 'US'),
        ('Celcuity Inc', 'CELC', 'XNAS', 'US'),
        ('CG Oncology, Inc.', 'CGON', 'XNAS', 'US'),
        ('Cytokinetics', 'CYTK', 'XNAS', 'US'),
        ('Eli Lilly and Company', 'LLY', 'XNYS', 'US'),
        ('Exelixis', 'EXEL', 'XNAS', 'US'),
        ('Ionis Pharmaceuticals, Inc.', 'IONS', 'XNAS', 'US'),
        ('Neumora Therapeutics, Inc.', 'NMRA', 'XNAS', 'US'),
        ('Novavax', 'NVAX', 'XNAS', 'US'),
        ('Ocular Therapeutix, Inc.', 'OCUL', 'XNAS', 'US'),
        ('PTC Therapeutics', 'PTCT', 'XNAS', 'US'),
        ('Revolution Medicines, Inc.', 'RVMD', 'XNAS', 'US'),
        ('Ultragenyx Pharmaceutical Inc', 'RARE', 'XNAS', 'US'),
        ('Vertex Pharmaceuticals Incorporated', 'VRTX', 'XNAS', 'US'),
        ('Viridian Therapeutics, Inc.', 'VRDN', 'XNAS', 'US'),

        -- Foreign issuers with US-listed ADRs
        ('AstraZeneca', 'AZN', 'XNAS', 'GB'),
        ('BioNTech SE', 'BNTX', 'XNAS', 'DE'),
        ('Novartis Pharmaceuticals', 'NVS', 'XNYS', 'CH'),
        ('Novo Nordisk A/S', 'NVO', 'XNYS', 'DK'),
        ('Sanofi', 'SNY', 'XNAS', 'FR'),
        ('Takeda', 'TAK', 'XNYS', 'JP'),

        -- Non-US primary listings
        ('Hoffmann-La Roche', 'ROG', 'XSWX', 'CH'),
        ('Ipsen', 'IPN', 'XPAR', 'FR'),

        -- Subsidiaries → public parent
        ('Janssen Research & Development, LLC', 'JNJ', 'XNYS', 'US'),
        ('Aragon Pharmaceuticals, Inc.', 'JNJ', 'XNYS', 'US'),
        ('Merck Sharp & Dohme LLC', 'MRK', 'XNYS', 'US'),
        ('Seagen, a wholly owned subsidiary of Pfizer', 'PFE', 'XNYS', 'US'),
        ('Alexion Pharmaceuticals, Inc.', 'AZN', 'XNAS', 'GB'),
        ('Bellus Health Inc. - a GSK company', 'GSK', 'XNYS', 'GB')
)
UPDATE entities AS e
SET primary_ticker = m.ticker,
    primary_mic    = m.mic,
    country        = m.country
FROM sponsor_map AS m
WHERE e.name = m.name
  AND e.primary_ticker IS NULL;

-- Register ticker_mic identifiers so future scanner emissions with a (ticker, mic)
-- hint resolve to these existing entities rather than creating duplicates.
INSERT INTO entity_identifiers (entity_id, id_type, id_value, priority)
SELECT e.id, 'ticker_mic', e.primary_ticker || '@' || e.primary_mic, 20
FROM entities e
WHERE e.name IN (
        'AbbVie','Amicus Therapeutics','Arcus Biosciences, Inc.','Arrowhead Pharmaceuticals',
        'Bristol-Myers Squibb','Celcuity Inc','CG Oncology, Inc.','Cytokinetics',
        'Eli Lilly and Company','Exelixis','Ionis Pharmaceuticals, Inc.',
        'Neumora Therapeutics, Inc.','Novavax','Ocular Therapeutix, Inc.','PTC Therapeutics',
        'Revolution Medicines, Inc.','Ultragenyx Pharmaceutical Inc',
        'Vertex Pharmaceuticals Incorporated','Viridian Therapeutics, Inc.',
        'AstraZeneca','BioNTech SE','Novartis Pharmaceuticals','Novo Nordisk A/S',
        'Sanofi','Takeda','Hoffmann-La Roche','Ipsen',
        'Janssen Research & Development, LLC','Aragon Pharmaceuticals, Inc.',
        'Merck Sharp & Dohme LLC','Seagen, a wholly owned subsidiary of Pfizer',
        'Alexion Pharmaceuticals, Inc.','Bellus Health Inc. - a GSK company'
    )
  AND e.primary_ticker IS NOT NULL
  AND e.primary_mic    IS NOT NULL
ON CONFLICT (id_type, id_value) DO NOTHING;
