# Strategy — UK LSE RNS (Regulatory News Service)

**Exchange:** London Stock Exchange
**MIC:** XLON
**Language:** English (native)
**Translation required:** No
**Build phase:** 1 (canary — first scanner built)
**Status:** SPEC VERIFIED — endpoints live-probed 2026-04-14. See §1.

---

## 1. Data source

London Stock Exchange's Regulatory News Service (RNS) is the mandatory disclosure channel for all main-market and AIM-listed companies. Filings are immediately public and cover the entire corporate-action and governance lifecycle.

### 1.1 Primary RNS enumeration — `investegate.co.uk` (verified 2026-04-14)

The LSE's own `www.londonstockexchange.com/news` page is an Angular SPA: the initial HTML is a ~55KB empty shell and all content is lazy-loaded via `api.londonstockexchange.com/api/v1/components/refresh`, which requires a proprietary POST payload shape that is not reverse-engineerable from the minified client bundle without a headless browser. **Do not use it as the primary RNS source.**

Instead: `https://www.investegate.co.uk/` provides a clean, stable HTML feed of every RNS announcement (plus RNS Reach, PRN, BUSINESS_WIRE mirrors) in reverse-chronological order. Verified live: 50 rows per page, each row exposing `date`, `supplier`, `TIDM (ticker)`, `company_name`, `headline`, and the canonical announcement URL in the shape:

```
https://www.investegate.co.uk/announcement/rns/{company-name-slug}--{tidm}/{headline-slug}/{article-id}
```

Pagination: `https://www.investegate.co.uk/?page=2`, `?page=3`, etc. The scanner paginates until the oldest row on a page falls outside the `window_days` window.

Rate behavior: no observed 429s at polite cadence (1 req / 2s). No auth. User-Agent header recommended.

### 1.2 Issuer metadata validation — `api.londonstockexchange.com` (verified)

Per-TIDM issuer metadata is available without auth at:

```
GET https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/{TIDM}
```

Returns a JSON blob with `isin`, `sedol`, `issuerName`, `sector`, `marketCap`, `tradingStatus`, `mic` fields. Used for two purposes: (1) sanity-check the TIDM extracted from investegate, (2) feed `isin` into OpenFIGI when ticker+MIC lookup fails (e.g., for AIM issuers that don't map cleanly).

Also verified usable for autocompletion / fuzzy TIDM lookup:

```
GET https://api.londonstockexchange.com/api/gw/lse/search/autocomplete?q={query}
```

### 1.3 FIGI resolution

OpenFIGI public endpoint with `idType=TICKER`, `idValue=<TIDM>`, `micCode=XLON`. Fallback to `idType=ID_ISIN` using the ISIN from §1.2. Cached for 7 days per `tools/openfigi_resolver.py`.

### 1.4 Filing body retrieval (Stage 2 only)

For signals that survive triage and need a body snippet, the scanner fetches the announcement URL from §1.1 — investegate's article pages serve the full RNS text in parseable HTML. The snippet (first 500 chars) populates `raw_data.snippet`; full body retrieval is deferred to the `deep-dives` skill.

## 2. Filing categories of interest

Not all RNS filings are interesting. The scanner filters to the categories that carry structural information for the mandate horizon:

| Category | RNS category code (to verify) | Signal type |
|----------|-------------------------------|-------------|
| Rule 2.7 — firm takeover offer | MSCH / takeover | `takeover_firm_offer` |
| Rule 2.4 — possible offer announcement | MSCH | `takeover_possible_offer` |
| TR-1 — major shareholder notification (>3%) | HOLD | `major_shareholder_change` |
| Form 3.1 — directors' and persons discharging managerial responsibility (PDMR) dealings | DSHP | `insider_dealing` |
| Profit warning | TRDG | `profit_warning` |
| Trading update (interim / pre-close) | TRDG | `trading_update` |
| AIM trading halt / suspension | SUSP | `aim_suspension` |
| Mining JORC resource/reserve update | JORC | `jorc_resource_update` |
| Scheme of arrangement — court sanction | SCHM | `scheme_sanction` |
| Results release (preliminary / interim / final) | RES | `results_release` |

Scanner must parse the RNS category code (structured in the feed) and the headline text. Body retrieval for full context happens on Stage 2 only (scored signals), not Stage 1.

## 3. Signal filters (Stage 1 triage)

- **Issuer is LSE main-market or AIM listed.** Exclude specialist funds and certain debt-only listings (which do appear on RNS).
- **Ticker + `.L` resolves via OpenFIGI.** Confirm FIGI + issuer_figi.
- **Market cap ≥ USD $300M** via yfinance lookup on `TICKER.L`.
- **Signal is novel** — dedup by `source_content_hash` vs. `signals/signal_log.json` within 30 days.
- **Source date within last 7 days.**
- **Headline is not a routine correction or housekeeping** — regex exclude list for terms like "replacement", "amendment to", unless the amendment changes a material fact.

## 4. Entity resolution

1. Extract ticker from RNS metadata (every RNS release has the issuer ticker in the structured header).
2. OpenFIGI: `{"idType": "TICKER", "idValue": "<ticker>", "micCode": "XLON"}`.
3. Resolve to FIGI + issuer_figi.
4. For UK issuers that are dual-listed (ADRs in US, or secondary HKEx listings), the issuer_figi ensures convergence with Tool 2's other scanners.

## 5. Signal output (common schema, per INSTRUCTIONS.md §2)

Every emitted signal includes:

- `upstream_system_id`: `"tool-2-non-us-primary"`
- `signal_id`: sha256 of `<ticker>|XLON|<source_date>|<rns_category>|<headline_hash>`
- `ticker_local`: e.g. `"ACME"`
- `mic`: `"XLON"`
- `ticker_plus_mic`: e.g. `"ACME.XLON"`
- `figi`, `issuer_figi` from OpenFIGI.
- `company_name_en`: from RNS metadata.
- `market_cap_usd_mm`: from yfinance.
- `exchange`: `"LSE"`
- `country`: `"GB"`
- `signal_type`: per table above.
- `signal_category`: coarse bucket (`takeover`, `governance`, `results`, `mining`, `shareholder`).
- `thesis_direction`: explicit where RNS category disambiguates (Rule 2.7 firm offer is `long` for target equity; profit warning is `short`). Ambiguous (generic trading update) is `unknown`.
- `strength_estimate`: 1–5 based on RNS category (Rule 2.7 = 5; trading update = 2).
- `source_url`, `source_content_hash`, `source_date`, `scan_date`.
- `translation_confidence`: `"n/a"` (English source).
- `raw_data`: `{ "rns_category_code": "...", "headline": "...", "snippet": "first 500 chars of body" }`

## 6. Deep dive checklist (Stage 3, when signal scores 28+)

- Read the full RNS body (not just headline).
- For Rule 2.7 offers: check offer terms (cash / share / mixed), offeror identity, financing, break fee, regulatory hurdles, irrevocable undertakings %.
- For profit warnings: magnitude vs. prior guidance, stated reason, comparable precedents for this issuer, covenant implications.
- For TR-1s: filer identity (is this a known activist, an index fund, a corporate holder?), direction (buy / sell / hold), threshold crossing.
- For JORC updates: resource-size delta, grade delta, mining-method change, counter-cyclical or pro-cyclical.
- Company context: market cap, sector, cross-listings, 30/90-day price, recent news.
- Web research layer: press coverage, analyst notes, litigation, regulatory FCA actions.
- Explicit kill conditions tied to RNS-observable events.
- Catalyst map with dates.

## 7. Tool file

`tools/lse_rns_scanner.py` — built in Phase 1 step 3.

## 8. Known risks and open questions

- **Endpoint stability — investegate.co.uk.** This is a third-party aggregator. If its HTML layout changes, the row-extraction regex breaks. Maintenance skill runs a daily health check that flags zero-row pages. Secondary fallback: `api.londonstockexchange.com/api/gw/lse/news/tidm/{TIDM}` per-issuer endpoint (observed working but only useful for targeted lookups, not firehose enumeration).
- **Ticker collisions with US tickers.** UK `ACME.L` may not be the same issuer as US `ACME`. Always key on ticker + MIC.
- **AIM market is noisier.** AIM companies often file frequent updates of limited materiality. Tune triage to require strength_estimate ≥ 3 for AIM issuers.
- **Cross-listed giants (HSBC, Unilever, Rio Tinto).** These will echo into HKEx, ASX, SEDAR scanners when those come online. D-001 / D-004 handle it.

## 9. Validation checklist (Phase 1 step 4 exit criteria)

- [ ] Scanner runs without errors on last-7-days window.
- [ ] At least 3 signals emitted in correct schema.
- [ ] Every emitted signal has valid FIGI + issuer_figi.
- [ ] Triage filter correctly rejects known-routine filings.
- [ ] Convergence engine accepts the signals without schema errors.
- [ ] At least one signal scores 28+ (real-world check — Rule 2.7 is common enough that one should appear in any given week).
- [ ] Candidate file produced via template with full deep dive and web research layer.
