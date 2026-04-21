# Strategy — Japan TDnet (Timely Disclosure Network)

**Exchange:** Tokyo Stock Exchange (primary), regional Japanese exchanges (Osaka, Nagoya, Fukuoka, Sapporo) via TDnet consolidated feed.
**MIC:** XTKS (TSE Prime/Standard/Growth)
**Language:** Japanese (native), some filings in English (Tanshin summaries increasingly bilingual).
**Translation required:** Yes — in-session Claude translation with confidence scoring per D-002.
**Build phase:** 2
**Status:** STUB — to build after UK is stable.

---

## 1. Data source

TDnet is Japan's consolidated timely-disclosure network operated by TSE. All listed Japanese companies must post material disclosures to TDnet in real-time during market hours.

**Planned primary endpoint (UNVERIFIED — probe at Phase 2):**

- `https://www.release.tdnet.info/inbs/I_list_001_001.html` — main disclosure list (HTML, refreshed every few minutes).
- Body access: each filing has a PDF link on TDnet; the PDF is the authoritative source for parsing.

Fallback: per-issuer disclosure pages on JPX or company IR sites.

## 2. Filing categories of interest

| Japanese name | English translation | Signal type |
|---------------|---------------------|-------------|
| 決算短信 (Kessan Tanshin) | Quarterly earnings summary | `earnings_tanshin` |
| 業績予想の修正 | Earnings guidance revision | `guidance_revision` |
| 業績予想の差異 | Forecast vs. actual variance | `forecast_variance` |
| 特別損失 | Extraordinary loss disclosure | `extraordinary_loss` |
| 重要事実 | Material fact disclosure | `material_fact` |
| 大量保有報告書 (5%+) | 5%+ shareholder report | `major_shareholder_change` |
| 公開買付 (TOB) | Tender offer | `tender_offer` |
| 株式分割/株式併合 | Stock split / consolidation | `stock_structure_change` |
| 業務提携/M&A | Business alliance / M&A | `ma_alliance` |
| MBO | Management buyout | `mbo` |

## 3. Signal filters (Stage 1 triage)

- **Issuer is TSE-listed** (Prime, Standard, Growth) — exclude unlisted JPX instruments.
- **Ticker + `.T` resolves via yfinance** for market-cap check.
- **Market cap ≥ USD $300M.**
- **Signal is novel** — dedup by content hash.
- **Source date within last 7 days.**
- **Translation confidence on critical passages ≥ 0.70** — else triage out.

## 4. Entity resolution (per D-003)

- TDnet structured metadata includes the 4-digit local ticker.
- OpenFIGI: `{"idType": "TICKER", "idValue": "7203", "micCode": "XTKS"}`.
- `issuer_figi` captures ADRs (many Japanese issuers have US ADRs) for cross-listing awareness.

## 5. Translation integrity (per D-002)

Two-pass confirmation for direction-driving passages:

1. First pass: full passage translation with a per-sentence confidence score.
2. Second pass: re-translate direction-critical phrases *only* (guidance numbers, negations, comparatives) and compare.
3. Confidence = minimum of the two passes on the critical span.
4. If confidence < 0.85 → `thesis_direction = unknown`, Signal Strength capped at 2.
5. If confidence < 0.70 → signal triaged out entirely.

Critical phrases to watch for flip errors: 増加/減少 (increase/decrease), 上回る/下回る (exceed/fall below), 予定/見込む (planned/expected), 否 (not), non-standard negation particles.

## 6. Signal output (common schema)

All fields per `INSTRUCTIONS.md §2`. Japanese-specific:

- `company_name_local`: original Japanese.
- `company_name_en`: translated.
- `translation_confidence`: per-signal, computed from direction-driving passages.
- `raw_data.filing_type`: Japanese category name in Romaji (e.g., "Tanshin_Q4").
- `raw_data.original_headline`: original Japanese headline.
- `raw_data.translated_headline`: English translation.

## 7. Deep dive checklist (Stage 3)

- Translate full filing body with confidence tracking.
- For Tanshin: compare new guidance vs. prior; compute implied growth deltas.
- For major shareholder reports: identify filer (domestic institution, foreign fund, individual), check cross-references with proxy-advisor databases.
- For TOB: offer price vs. current, offeror identity, minimum tender condition, break-up fees.
- Web research layer: Nikkei, Reuters Japan, Bloomberg Japan, Toyo Keizai, general press.
- Cross-listing check — does the issuer have a US ADR or LSE listing that might echo?
- Explicit kill conditions tied to TDnet-observable future filings.

## 8. Tool file

`tools/tdnet_scanner.py` — Phase 2.

## 9. Known risks

- **HTML parsing fragility.** TDnet list is HTML; schema changes occasionally. Maintenance task monitors.
- **PDF body parsing.** Many Tanshin bodies are PDFs with mixed Japanese layouts. Use `pdfplumber` or similar; accept that some filings will extract poorly and need fallback snippets.
- **Translation cost.** In-session Claude translation is the most expensive step. Budget: ~60 seconds per filing body. Triage before translation — only translate filings that pass headline-level filters.
- **Small-cap liquidity.** Japan has many $300M – $500M names with thin liquidity. Stage 1 triage checks yfinance average volume in addition to market cap.

## 10. Validation checklist (Phase 2 exit)

- [ ] Scanner runs without errors on last-7-days window.
- [ ] Translation confidence emitted per signal.
- [ ] OpenFIGI resolution succeeds on Japanese tickers.
- [ ] At least one signal with `thesis_direction = unknown` (confirming D-002 default behavior works).
- [ ] At least one signal with `thesis_direction != unknown` at confidence ≥ 0.85.
- [ ] Candidate produced for a 28+ convergent or high-confidence direct signal.
