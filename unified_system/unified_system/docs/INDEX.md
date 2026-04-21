# Index — Unified Investment Discovery System

Quick reference to every file in the system, grouped by function.

---

## State & Protocol
- `SESSION_STATE.md` — the relay baton (read first every session)
- `INSTRUCTIONS.md` — architecture, pipeline, session rules
- `OBJECTIVES.md` — mandate + success criteria
- `CONTEXT.md` — API endpoints + integration reference
- `DECISIONS.md` — architectural decisions log
- `OPEN_QUESTIONS.md` — unresolved investigations
- `PROGRESS_LOG.md` — append-only session history
- `SESSION_LOCK.md` — operational lock (created when operational/maintenance tasks run)

## Framework
- `framework/profile_merger_arb.md` — scoring rubric for merger arbitrage
- `framework/profile_activist_governance.md` — scoring rubric for activist + governance
- `framework/profile_binary_catalyst.md` — scoring rubric for FDA + binary regulatory
- `framework/profile_short_positioning.md` — scoring rubric for short flow + insider clusters
- `framework/profile_litigation.md` — scoring rubric for litigation
- `framework/candidate_template.md` — candidate dossier template

## Strategy Specs
- US: `strategies/us_edgar_keyword.md`, `strategies/us_fda_pdufa.md`, `strategies/us_congressional.md`
- EU: `strategies/eu_esma_short.md`
- UK: `strategies/uk_lse_rns.md`
- Japan: `strategies/jp_tdnet.md`
- Australia: `strategies/au_asx.md`
- Canada: `strategies/ca_sedar.md`
- Hong Kong: `strategies/hk_hkex.md` (planned scanner)
- Korea: `strategies/kr_kind.md` (planned)
- India: `strategies/in_bse_nse.md` (planned)
- Brazil: `strategies/br_cvm.md` (planned)
- Mexico: `strategies/mx_bmv.md` (planned)
- Litigation: `strategies/lit_federal_civil.md`, `strategies/lit_sec_enforcement.md` (both planned scanners)
- Litigation deferred: `strategies/lit_delaware_chancery.md`, `strategies/lit_itc_337.md`, `strategies/lit_ptab_ipr.md`, `strategies/lit_doj_ftc_antitrust.md`

## Tools
- Pipeline: `tools/pipeline_runner.py`, `tools/run_post_scan.py`, `tools/run_scanner.py`
- Shared utilities: `tools/openfigi_resolver.py`, `tools/http_client.py`, `tools/mcap_cache.py`, `tools/party_resolver.py`, `tools/build_exhibit21_map.py`
- Analysis: `tools/convergence_engine.py` (multi-profile)
- Reporting: `tools/report_generator.py` (reportlab PDF composer)
- US scanners: `tools/edgar_filing_monitor.py`, `tools/esma_short_scanner.py`, `tools/fda_pdufa_pipeline.py`, `tools/congressional_trading.py`
- Non-US scanners (operational): `tools/lse_rns_scanner.py`, `tools/tdnet_scanner.py`, `tools/asx_scanner.py`, `tools/sedar_plus_scanner.py`
- Non-US scanners (planned): `tools/hkex_scanner.py`, `tools/kind_scanner.py`, `tools/bse_nse_scanner.py`, `tools/cvm_scanner.py`, `tools/bmv_scanner.py`
- Litigation scanners (planned): `tools/courtlistener_scanner.py`, `tools/sec_enforcement_scanner.py`
- Helpers: `tools/ca_universe.py`, `tools/asx_universe.py`, `tools/asx_chunked_scan.py`, `tools/asx_finalize.py`, `tools/asx_rubric.py`, `tools/sedar_rubric.py`, `tools/sedar_chrome_supplement.py`, `tools/jpx_market_cap.py`, `tools/boilerplate_filters.py`

## Config
- `config/scanner_registry.json` — cadences, endpoints, last_run, scoring_profile mapping
- `config/entity_cache.json` — unified OpenFIGI + party resolution cache

## Data
- `signals/signal_log.json` — rolling unified signal log (14-day live window + historical)
- `signals/legacy_t1/` — migrated Tool 1 signal files
- `signals/legacy_t2/` — migrated Tool 2 signal files
- `working/openfigi_cache/` — per-query OpenFIGI response cache
- `working/jpx_mcap_cache.json` — JPX market cap cache
- `working/asx_universe.json` — ASX universe snapshot
- `working/ca_universe.json` — Canadian universe (to be built — Q-006)
- `esma_snapshots/` — daily ESMA position snapshots (historical tracking per Q-008)

## Candidates
- `candidates/*.md` — active candidate dossiers (33 total at scaffold time)
- `candidates/watchlist/*.json` — machine-readable watchlist (10 at scaffold time)
- `candidates/delivered/*.md` — resolved outcomes (1 at scaffold time — TVTX)
- `candidates/archive/*.md` — superseded dossiers

## Reports (read-only area for unified-reporting task)
- `reports/REPORTING_LOCK.md` — independent reporting lock
- `reports/candidates_index.json` — machine-readable candidate registry
- `reports/daily/*.pdf` — daily digest PDFs
- `reports/weekly/*.pdf` — weekly strategic report PDFs
- `reports/dossiers/pdf/*.pdf` — per-candidate dossier PDFs
- `reports/working/*.log` — reporting-task issue logs (read-only surfacing of operational issues)

## Research & Baselines
- `research/*.md` — persistent investigative notes
- `baselines/exhibit21_subsidiary_table.json` — Exhibit 21 parent-subsidiary map (for litigation)
- `baselines/party_cache.json` — party resolution cache (litigation)

## Archive
- `archive/*` — superseded files (never delete, always rename with date suffix)
- See `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\_ARCHIVED_*_2026-04-16\` for the six legacy project folders.
