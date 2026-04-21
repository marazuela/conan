# Strategy — Australia ASX Announcements

**Exchange:** Australian Securities Exchange
**MIC:** XASX
**Language:** English
**Translation required:** No
**Build phase:** 3
**Status:** ACTIVE — endpoints verified 2026-04-14.

---

## 1. Data source

ASX Market Announcements platform. All ASX-listed companies file material announcements here in real-time. Strong disclosure culture: mandatory continuous disclosure regime (ASX Listing Rule 3.1).

**Verified endpoint (2026-04-14):**

- `https://asx.api.markitdigital.com/asx-research/1.0/companies/{TICKER}/announcements` — per-company announcement JSON. Returns latest ~5 items regardless of `count`/`limit`/`days`/`startDate`/`pageSize` parameters (server-side cap). Fields: `announcementType`, `date` (ISO 8601), `documentKey`, `headline`, `isPriceSensitive`, `numPages`, `url` (empty).
- `https://www.asx.com.au/asx/1/company/{TICKER}/announcements?count=20` — alternate per-company endpoint (ASX CDN), returns up to 20 items; same fields plus a working `url` for the PDF.

**Universe-enumeration strategy (no firehose available):**

No single-URL firehose works without auth. The scanner operates in a poll-per-ticker pattern against a pre-filtered universe of ASX 300 / ≥USD-300M companies (see `working/asx_universe.json`, refreshed weekly).
- Universe source: `https://www.asx.com.au/asx/research/ASXListedCompanies.csv` (public CSV of all listed companies with GICS + market cap) → filter by market cap ≥ $300M USD equivalent.
- Polling cadence: daily scan of universe × per-company endpoint. With ~200-300 tickers, one run is 200-300 HTTP calls in ~3-5 minutes with rate-limit throttling.

## 2. Filing categories of interest

| ASX form/type | Signal type |
|---------------|-------------|
| Drilling results (mining/resources) — JORC-compliant | `jorc_drilling_results` |
| Resource/reserve update (JORC) | `jorc_resource_update` |
| Appendix 4C (quarterly cash-flow report for early-stage companies) | `appendix_4c_cashflow` |
| Appendix 4D (half-year report) | `half_year_report` |
| Appendix 4E (preliminary final report) | `preliminary_final_report` |
| Form 603 (notice of initial substantial holder) | `substantial_holder_initial` |
| Form 604 (change in substantial holder) | `substantial_holder_change` |
| Form 605 (ceasing to be substantial holder) | `substantial_holder_ceasing` |
| Takeover bid announcement | `takeover_bid` |
| Trading halt | `trading_halt` |
| Trading suspension | `trading_suspension` |
| Profit guidance update | `profit_guidance` |
| Capital raising announcement (placement, SPP, rights issue) | `capital_raising` |

## 3. Signal filters (Stage 1 triage)

- ASX main board listed (exclude NSX, Chi-X and secondary boards).
- Ticker + `.AX` resolves via yfinance.
- Market cap ≥ USD $300M. For mining-heavy universe: junior miners often sit below the floor; they're excluded by design.
- Signal novelty + 7-day freshness + Appendix-4C-specific cash-runway filter (≥2 quarters runway required to avoid pre-distress noise, unless the distress itself is the signal).

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<ticker>", "micCode": "XASX"}`.

Cross-listing awareness: BHP (ASX + LSE), Rio Tinto (ASX + LSE + SEDAR), Westpac (ASX + NYSE ADR). D-001 dedup engages.

## 5. Signal output

Standard schema. `translation_confidence = "n/a"`. ASX structured forms provide clean metadata; `raw_data.asx_form_code` captures the specific Appendix or Form number.

## 6. Deep dive checklist

- For JORC drilling results: grade, width, depth, hole count, assay timeline, location within known resource envelope, drill-rig availability.
- For Appendix 4C: cash balance, net operating cash flow, projected quarters of runway, capital raising plans.
- For substantial holder changes: filer identity (super fund, activist, foreign strategic), direction (accumulating / divesting).
- For takeover bids: bid structure, competing bid potential, scheme vs. off-market bid.
- Web research layer: AFR, The Australian, mining trade press for JORC context.
- Cross-listing check.

## 7. Known risks

- **Junior miner noise.** Australia has thousands of exploration-stage juniors. $300M floor and listing-board filter cut most; remaining noise managed by strength_estimate thresholds.
- **JORC expertise.** Assessing drilling results requires geological context. Deep-dive flags when results are unambiguously material vs. exploratory — manual review when ambiguous.

## 8. Tool file

`tools/asx_scanner.py` — Phase 3.
