# Strategy — Canada SEDAR+

**Exchange:** Toronto Stock Exchange (TSX), TSX Venture Exchange (TSXV)
**MIC:** XTSE (TSX), XTSX (TSXV)
**Language:** English and French (filings may be in either — both are authoritative)
**Translation required:** Only for French-language filings — in-session translation with confidence scoring per D-002.
**Build phase:** 4
**Status:** ENDPOINT PROBE COMPLETE (2026-04-14) — direct SEDAR+ is bot-blocked. Scanner must use a universe-enumeration pattern (same shape as Phase 3 ASX). Fleshing out in progress.

---

## 1. Data source — probe results (2026-04-14)

SEDAR+ is Canada's consolidated filing system (replaced legacy SEDAR in 2023). All TSX/TSXV-listed issuers file continuous-disclosure documents here.

**Endpoint probe summary:**

| Source | Status | Verdict |
|--------|--------|---------|
| `https://www.sedarplus.ca/csa-party/records/` | Blocked — PerfDrive JS challenge on every request | Unusable without a headless browser |
| Per-issuer SEDAR+ profile pages | Blocked — same challenge | Unusable via raw HTTP |
| `newswire.ca` category pages | Accessible but 215K-char HTML, no RSS, no date filter, no pagination | Viable only as a fallback firehose with heavy parsing cost |
| **yfinance `Ticker('<SYM>.TO').news`** | **Works — returned 10 dated items with title, provider, URL on SHOP.TO probe** | **Primary endpoint for Phase 4** |
| Globe Investor, BNN Bloomberg | Accessible but rate-limited; inconsistent structured data | Reserved for Stage-2 deep-dive web research layer |

**Chosen approach:** Universe-enumeration, same pattern as Phase 3 ASX.

1. `tools/ca_universe.py` builds a TSX+TSXV universe of issuers ≥ $300M USD, cached 7 days in `working/ca_universe.json`.
2. `tools/sedar_scanner.py` iterates the universe, calls `yfinance.Ticker(<T>.TO|.V).news` per ticker, filters to window, and classifies headlines against `SEDAR_TITLE_RULES`.
3. Per-ticker throttle ~0.3 s (yfinance self-throttles; matches ASX budget).

**Known compromise:** yfinance news is an aggregator feed (StockStory, Zacks, Reuters, CP), not the raw SEDAR+ material-change firehose. We will miss some non-English and non-syndicated filings. Acceptable for Phase 4 MVP; deep-dive can still pull the raw SEDAR+ PDF via Claude-in-Chrome if a signal warrants it.

## 2. Filing categories of interest

| SEDAR+ filing type | Signal type |
|--------------------|-------------|
| Material change report (Form 51-102F3) | `material_change_report` |
| Early warning report (10%+ ownership) | `early_warning_10pct` |
| NI 43-101 technical report (mining) | `ni43101_technical_report` |
| NI 51-101 reserves report (oil & gas) | `ni51101_reserves` |
| Take-over bid circular | `takeover_bid_circular` |
| Directors' circular (response to bid) | `directors_circular` |
| Management information circular (proxy) | `proxy_circular` |
| Q1/Q2/Q3 interim MD&A | `interim_mda` |
| Annual MD&A / AIF | `annual_mda` |
| Plan of arrangement | `plan_of_arrangement` |
| Cease trade order | `cease_trade_order` |

## 3. Signal filters (Stage 1 triage)

- TSX main board preferred; TSXV accepted only when strength_estimate ≥ 4 (venture board is noisy).
- Ticker + `.TO` (TSX) or `.V` (TSXV) resolves via yfinance.
- Market cap ≥ USD $300M. Mining/oil & gas juniors on TSXV almost always excluded by floor.
- Dual-language dedup: if the same material change report is filed in both English and French versions, dedup by `source_content_hash` computed after language detection.

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<ticker>", "micCode": "XTSE"}` or `"XTSX"`.

Cross-listing awareness: many Canadian issuers dual-list on NYSE/NASDAQ (Shopify, Barrick, Suncor, Canadian National). D-001 dedup engages against Tool 1's US signals where relevant (but Tool 1 and Tool 2 are independent — cross-tool dedup is out of scope for Phase 0).

## 5. Translation integrity (French filings only)

Per D-002. Critical flip-error phrases:
- augmenter/diminuer (increase/decrease)
- supérieur à / inférieur à (above / below)
- prévu/anticipé (planned/anticipated)
- ne ... pas (French negation)

Confidence <0.85 → `thesis_direction = unknown`.

## 6. Signal output

Standard schema. `translation_confidence` = `"n/a"` for English filings, computed for French. `raw_data.filing_language` captures `en` or `fr`.

## 7. Deep dive checklist

- For NI 43-101 technical reports: mineral resource category (measured/indicated/inferred), grade vs. peer, metallurgical recovery, capex estimate, permitting status.
- For early warning reports: filer identity (institutional, activist, strategic), 10% vs. 20% threshold crossing.
- For plans of arrangement: court hearing dates, shareholder vote date, competing-bid protection.
- Web research layer: Globe and Mail, Financial Post, Northern Miner (mining), BNN Bloomberg.
- Cross-listing check with US exchanges.

## 8. Known risks

- **TSXV noise.** Venture board has thousands of micro-cap mining and cannabis names. $300M floor and strength ≥ 4 filter cut most.
- **Dual-language duplication.** Same filing in EN + FR must not count as convergence. Dedup on content hash after language normalization.
- **Cross-border dual listings.** Major Canadian issuers often file equivalent US 8-Ks. Scanner should flag `cross_listed_on: ["XNYS"]` or similar.

## 9. Tool file

`tools/sedar_scanner.py` — Phase 4.
