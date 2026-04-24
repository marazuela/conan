-- CourtListener selectivity rework (2026-04-24).
--
-- Problem: the scanner was emitting ~19 signals/day with 99% of resolved
-- entities having a case caption as their `name` (e.g. "Sipin v. Tesla Inc."),
-- 0 FIGI coverage, and 66% archive band. NOS 190 (Other Contract) + 830/835
-- (Patent) together were 70% of emissions and 70%+ archive — low-value noise.
--
-- Fix delivered in code:
--   - Shared caption_party.extract_corporate_party (strip gov plaintiff, "et al",
--     handle in-re / v. splits); wired into courtlistener + delaware_chancery
--     scanners.
--   - Shared sec_issuer_lookup.IssuerIndex (SEC's company_tickers.json)
--     resolves extracted party → ticker/CIK at scanner time.
--   - Per-NOS config: NOS 190 priority=off (disabled); 830/835 require
--     universe match; 850/410 emit unconditionally (higher strength).
--   - Split signal types: federal_civil_{securities,antitrust,patent,contract}_filed
--   - Rubric: party_confidence_cap threshold raised from <3 to <4;
--     new universe_miss_cap archives litigation signals without a resolved
--     ticker unless NOS is securities/antitrust or signal is Chancery.
--   - Fanout subject: when ticker is NULL, show entity.name (truncated)
--     instead of "?.?".
--
-- This migration registers the new signal types in the courtlistener scanner
-- row's profile map and adds a `courtlistener_nos_190_enabled` config flag
-- (default false). The flag is a manual ops switch for re-enabling NOS 190
-- once caption-parsing precision is proven.
--
-- Idempotent: jsonb `||` merges preserve existing keys. Re-running appends
-- rather than replaces.

UPDATE public.scanners
SET signal_type_profile_map = COALESCE(signal_type_profile_map, '{}'::jsonb) || jsonb_build_object(
      -- New NOS-specific signal types
      'federal_civil_securities_filed', 'litigation',
      'federal_civil_antitrust_filed',  'litigation',
      'federal_civil_patent_filed',     'litigation',
      'federal_civil_contract_filed',   'litigation',
      -- Procedural variants (unchanged profile, same as federal_civil_filed)
      'class_certified',                'litigation',
      'settlement',                     'litigation',
      'summary_judgment',               'litigation',
      'mtd_denied',                     'litigation',
      -- Keep legacy federal_civil_filed registered for any in-flight signals
      'federal_civil_filed',            'litigation'
    ),
    config = COALESCE(config, '{}'::jsonb) || jsonb_build_object(
      'courtlistener_nos_190_enabled', false,
      'courtlistener_nos_overrides',   '{}'::jsonb
    )
WHERE name = 'courtlistener_scanner';

-- Chancery already has these signal types wired up in its own earlier
-- migration; nothing to do here for that scanner. Shared helpers come
-- online at the application layer automatically.
