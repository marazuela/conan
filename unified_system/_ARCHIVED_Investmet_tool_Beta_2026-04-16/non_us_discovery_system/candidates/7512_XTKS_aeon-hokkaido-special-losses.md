---
ticker_local: "7512"
mic: XTKS
ticker_plus_mic: 7512.XTKS
isin: null
figi: BBG000H7HQW4
issuer_figi: BBG000H7HQ72
company_name_local: "イオン北海道"
company_name_en: "Aeon Hokkaido Corporation"
market_cap_usd_mm: 759
exchange: TDnet
country: JP
score: 31.0
convergence_bonus: 0
score_total: 31.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: 0.88
first_signal_date: 2026-04-09
last_updated: 2026-04-15
primary_catalyst_date: 2026-04-09
cross_listed_on: []
related_signal_ids: []
signal_type: impairment_loss
signal_category: results
scanner: tdnet
---

# AEON Hokkaido (7512.XTKS) — Special losses booking

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-09 (04:30 UTC / 13:30 JST) AEON Hokkaido — the Hokkaido-regional supermarket/GMS operator within the AEON group — released a TDnet filing titled 「特別損失の計上に関するお知らせ」 ("Notice of booking of special losses"). Scanner classifies signal_strength=4, thesis_direction=short, tc=0.88. Score 31 — immediate route. At $759M USD market cap, comfortably above the floor but still small-cap by global standards. The single-topic filing (no offsetting non-op income) is typical of store-closure impairment at a regional GMS operator facing secular footfall decline in regional Japan.

## Source signal

- **Source URL**: https://www.release.tdnet.info/inbs/140120260408500265.pdf
- **Filing date/time**: 2026-04-09 04:30 UTC (previous-day docID suggests 2026-04-08 filing released 2026-04-09)
- **Local-language headline**: 「特別損失の計上に関するお知らせ」
- **Signal ID**: `0d8a11326fdbca771918ce088f9264ff`

## Translation notes

Translation confidence 0.88 — above the 0.85 direction-allow floor. Scanner tag `short` reflects the `特別損失` pattern. Deep-dive MUST read the PDF to confirm:
- Actual yen magnitude
- Whether this is store-closure impairment (most likely for a regional GMS), restructuring, or PP&E writedown
- Any concurrent FY guidance revision

## Company context (to be completed by deep-dive)

- Market cap: $759M USD (JPY ~¥114B). Mid-cap by Japan standards.
- Sector: Food & Staples Retailing (supermarkets / GMS).
- Parent/affiliate structure: AEON group (parent 8267). AEON operates several regional listed subsidiaries including 7512 (Hokkaido), 8273 (Kyushu), 8278 (Daiei — delisted), 2653 (Liquor — delisted).
- Fiscal year: AEON group FY ends February. April 2026 filings relate to FY2026 close.
- Business: ~130 stores in Hokkaido under AEON / MaxValu / DAIEI banners. Secular pressure from depopulation of regional Hokkaido + wage inflation on store-level opex.
- 30/90-day price: TODO. `7512.T` via yfinance.
- Recent news: TODO — check for store-closure announcements in Hokkaido press (Hokkaido Shimbun, Nikkei regional).

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged `short` — standard TDnet 特別損失 pattern without an offsetting income item.

Deep-dive must:
- Read the PDF to extract:
  - Special loss amount (JPY)
  - Classification (impairment of tangible fixed assets? closure cost? inventory write-down?)
  - Number of stores affected (if store-closure)
  - Whether concurrent FY2026 (Feb-end) guidance revision was filed
- Compare loss magnitude to consensus FY2026 net income (BBG JP consensus)
- Check whether 8267 AEON parent filed a companion release on the same day (group-level impact)

## Steelman of the opposite view

Regional GMS operators in Japan (7512 Hokkaido, 8273 Kyushu) periodically book store-closure impairments as part of portfolio rationalization; these are **typically pre-flagged in the medium-term plan and already in analyst estimates**. If:
- The loss is consistent with the previously-disclosed "restructuring reserve" budget
- The closures improve store-level ROIC (closing loss-making stores is a long-term positive)
- Management simultaneously raises the FY2027 operating-profit outlook on the improved store portfolio
...then the stock can rally on the release as a "kitchen-sinking" signal. Deep-dive must check whether 7512's existing multi-year plan already included store closures at this scale.

## Web research layer (deep-dive TODO)

- Search Nikkei for "イオン北海道 店舗閉鎖" (AEON Hokkaido store closure) in last 90 days
- Check AEON Hokkaido IR page at https://www.aeon-hokkaido.jp/ir/
- Cross-reference 8267 parent AEON filings on the same day
- Check sister-company 8273 AEON Kyushu for parallel filings (group-wide restructuring signal vs. Hokkaido-idiosyncratic)

## Kill conditions

- **Kill if** special loss < ¥1B (immaterial vs. ¥114B market cap)
- **Kill if** guidance HELD or RAISED on the same day
- **Kill if** closure plan was pre-announced in the medium-term plan
- **Kill if** 20-day ADV < $1M USD — untradeable
- **Kill if** stock already down >10% in 30 days pre-filing
- **Kill if** deep-dive finds the loss is non-recurring and management guides to improved FY2027 margins

## Catalyst map

- **Primary catalyst**: FY2026 results release (typically early April for Feb-end FY) — watch concurrent guidance revision
- **Window**: 0–21 days
- **Entry trigger**: deep-dive confirms material loss + guidance cut
- **Exit trigger**: earnings-day reversal or kill condition hit

## Position sizing

Placeholder. 7512 liquidity is thin; 20-day ADV check required. Target: 0.5–1.0% net short on high conviction, halved at tc=0.88.

## Source traceability

- Signal hash: `dfb1d87ada8533f9c4740f38`
- TDnet PDF: https://www.release.tdnet.info/inbs/140120260408500265.pdf
- Scanner: `tdnet` / Phase 2
- Scan date: 2026-04-15
