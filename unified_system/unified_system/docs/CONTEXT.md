# Context — API Endpoints & Integration Reference

Consolidated reference for every primary data source across the system. Endpoints are live as of 2026-04-16 unless noted.

---

## US Sources

### SEC EDGAR (EFTS + data.sec.gov)
- **EFTS full-text search**: `https://efts.sec.gov/LATEST/search-index?q=<query>&dateRange=custom&startdt=...&enddt=...&forms=<type>`
- **Company facts**: `https://data.sec.gov/submissions/CIK<10-digit>.json`
- **Company tickers**: `https://www.sec.gov/files/company_tickers.json`
- **User-Agent required**: `"Pedro Research pedro@example.com"` format. Max 10 req/sec.
- **Notes**: EFTS has a 35s wall-clock budget in this sandbox (D-018 from Tool 1).
- **Used by**: edgar_filing_monitor, fda_pdufa_pipeline (for EDGAR-based PDUFA discovery), sec_enforcement_scanner, courtlistener_scanner (party resolution).

### ESMA / National Regulators (short disclosures)
- **FCA** (UK): `https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx`
- **AMF** (FR): `https://www.amf-france.org/sites/default/files/doc_vente_a_decouvert/VAD%20Publication.csv`
- **AFM** (NL): JSON endpoint (see esma_short_scanner.py)
- **BaFin** (DE): `https://portal.mvp.bafin.de/database/ShortPositions/..` (CSV download)
- **CNMV** (ES): **Q-002 — access blocked; Pedro's home market, high priority to unblock**
- **CONSOB** (IT): PDF-based; not scraped
- **Aggregation**: ESMA central register inconsistent across states, so scan each national regulator.

### USAspending.gov (retired — sam_gov_contracts scanner deprecated)
- Retired per UNIFIED_SYSTEM plan. contract_monitor produced zero actionable output over 67 sessions.

### Capitol Trades (Congressional)
- **Endpoint**: `https://www.capitoltrades.com/trades?txDate=...` (HTML scrape)
- **Notes**: Ro Khanna filter active (Q-014 — high-volume low-signal filer).

### openFDA + ClinicalTrials.gov
- **openFDA**: `https://api.fda.gov/drug/label.json?search=...`
- **ClinicalTrials.gov v2 API**: `https://clinicaltrials.gov/api/v2/studies?query.term=...`
- **Notes**: ClinicalTrials.gov returns 403 in this sandbox (egress restriction). Pipeline falls back to EDGAR PDUFA discovery via 8-K + PR search.

### SEC Litigation Releases
- **Index**: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=34&dateb=&owner=include&count=40`
- **Litigation releases**: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=LITIGATION&owner=include&count=40`
- **Better**: `https://www.sec.gov/litigation/litreleases/litrelarchive/litarchive2026.shtml` (HTML archive by year).

### CourtListener RECAP (litigation)
- **API v4**: `https://www.courtlistener.com/api/rest/v4/`
- **Docket endpoint**: `/dockets/?q=...&order_by=date_filed`
- **Token**: free, register at courtlistener.com. User-Agent + token required.
- **Rate**: ~5000 req/hr.

---

## UK

### LSE RNS (Regulatory News Service)
- **Endpoint**: `https://www.londonstockexchange.com/api/gw/lse/announcements?format=json&page=<n>&pagesize=<n>&dateFrom=...&dateTo=...`
- **All-data cache**: Maintenance task pre-warms `lse_alldata_cache/` so operational runs with wider windows don't hit cold-sandbox timeouts.
- **UK Takeover Code**: Rule 2.4 (possible offer) → Rule 2.7 (firm offer). Historical 2.4→2.7 conversion ~55%.

---

## Japan

### TDnet
- **Endpoint**: `https://www.release.tdnet.info/inbs/I_list_001_{YYYYMMDD}.html` (HTML scrape)
- **Ticker format**: 4-digit primary, 5-digit alphanumeric for preferred/special classes (e.g., `469A0` → `.T` resolves as `469A.T` after stripping trailing `0`).
- **Known defect (D-open)**: FIGI-resolve 404s on 5-char alphanumeric tickers. Fix = strip trailing `0` when `len(ticker)==5` AND `ticker[3].isalpha()`.
- **JPX market cap cache**: `working/jpx_mcap_cache.json` — persisted across sessions.

---

## Australia

### ASX Announcements
- **Endpoint**: `https://www.asx.com.au/asx/statistics/announcements.do` (HTML + JSON pagination)
- **Universe cache**: `working/asx_universe.json` — refreshed weekly.
- **Chunked processing**: `asx_chunked_scan.py` handles large-day volumes via cursor in `working/asx_chunked_state.json`.

---

## Canada

### SEDAR+
- **Endpoint**: `https://www.sedarplus.ca/csa-party/records/document.html?id=...`
- **Blocker**: `working/ca_universe.json` not yet built. Build via `python3 -m tools.ca_universe --throttle 0.2 --boards tsx,tsxv`.
- **Supplement**: `sedar_chrome_supplement.py` handles JS-rendered pages that plain requests can't parse.

---

## Hong Kong

### HKEx
- **Endpoint**: `https://www1.hkexnews.hk/listedco/listconews/sehk/{yyyy}/{mmdd}/{stock}_{time}.htm`
- **Announcement search**: `https://www.hkexnews.hk/app/index.html`
- **Status**: Scanner planned (Phase 5). Strategy spec in `strategies/hk_hkex.md`.

---

## Korea

### KIND (Korea Investors Network for Disclosure System)
- **Endpoint**: `https://kind.krx.co.kr/disclosure/`
- **Status**: Scanner planned. Strategy in `strategies/kr_kind.md`.

---

## India

### BSE + NSE
- **BSE**: `https://www.bseindia.com/corporates/ann.aspx`
- **NSE**: `https://www.nseindia.com/api/corporate-announcements?index=...`
- **Status**: Scanner planned. Strategy in `strategies/in_bse_nse.md`.

---

## Brazil

### CVM (Comissão de Valores Mobiliários)
- **Endpoint**: `https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/`
- **Status**: Scanner planned. Strategy in `strategies/br_cvm.md`.

---

## Mexico

### BMV (Bolsa Mexicana de Valores)
- **Endpoint**: `https://www.bmv.com.mx/es/emisoras/eventos-relevantes`
- **Status**: Scanner planned. Strategy in `strategies/mx_bmv.md`.

---

## Entity Resolution

### OpenFIGI v3 API
- **Endpoint**: `https://api.openfigi.com/v3/mapping`
- **Auth**: no auth for unlimited free calls but limited; API key (free registration) gets 25 req/6s.
- **Request body**: list of `{"idType": "TICKER"|"ISIN"|"CUSIP"|"BASE_TICKER", "idValue": "...", "micCode": "..."}`.
- **Response**: `figi`, `ticker`, `compositeFIGI`, `shareClassFIGI`. Use `compositeFIGI` as `issuer_figi` for convergence.
- **Cache**: `working/openfigi_cache/` (one JSON per query) + `config/entity_cache.json` for the unified cache.

### Market Cap (yfinance)
- **Library**: `yfinance.Ticker(symbol).fast_info['market_cap']`
- **Cache**: `tools/mcap_cache.py` wraps with 24h TTL per ticker.

---

## Rate Limits & Etiquette

- **SEC**: 10 req/sec; User-Agent with contact email mandatory.
- **OpenFIGI**: 25 req/6s with API key; 25 req/60s without.
- **Capitol Trades**: be gentle — 1 req/sec soft limit.
- **LSE RNS**: no explicit limit, but timeouts common. Use `http_client.py` with backoff.
- **yfinance**: no hard limit but throttle to 1/sec on market-cap batches.

---

## Common Failure Modes

| Source | Failure | Mitigation |
|--------|---------|------------|
| ClinicalTrials.gov | 403 in sandbox | EDGAR PDUFA discovery fallback |
| CNMV (ES) | Access blocked | Q-002 — investigate |
| OpenFIGI | 404 on 5-char JP tickers | Strip trailing `0`, retry |
| SEDAR+ | No universe | Build `working/ca_universe.json` |
| Tor-like responses | Bot detection | Rotate User-Agent, add delay |
| LSE wide-window | Timeout in cold sandbox | Pre-warm `lse_alldata_cache/` in maintenance |
