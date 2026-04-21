---
ticker_local: "8267"
mic: XTKS
ticker_plus_mic: 8267.XTKS
isin: null
figi: BBG000BN0FT1
issuer_figi: BBG000BN0FD8
company_name_local: イオン
company_name_en: "Aeon Co., Ltd."
market_cap_usd_mm: 30388
exchange: TDnet
country: JP
score: 35.0
convergence_bonus: 0
score_total: 35.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: 0.92
first_signal_date: 2026-04-15
last_updated: 2026-04-15
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["340f9fef654c51e2ec31ddb83d6e8b61", "b789ac16eb8d344248d72d1538cbcd40"]
signal_type: tender_offer
signal_category: takeover
scanner: tdnet
---

# Aeon Co., Ltd. (8267.XTKS) — Tender Offer

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

TDnet filed 2026-04-15 for Aeon Co., Ltd. (8267). Scanner classified signal_type=tender_offer at score_total=35.0, translation_confidence=0.92, thesis_direction=long. Market cap $30388M USD. 2 related filing(s) merged via D-004 convergence.

## Source signals

- [tender_offer] https://www.release.tdnet.info/inbs/140120260415504769.pdf — signal_id 340f9fef654c51e2ec31ddb83d6e8b61
- [impairment_loss] https://www.release.tdnet.info/inbs/140120260409500830.pdf — signal_id b789ac16eb8d344248d72d1538cbcd40

## Translation notes

Japanese source. Translation pattern is unambiguous at tc=0.92. Deep-dive must still read the PDF for specifics (price, premium, timing, counterparty).

## Company context

- Market cap: $30388M USD
- Sector / sub-sector: TODO (deep-dive)
- Recent price action: TODO (30/90d)
- Cross-listings: TODO (check HK / ADR / LSE)

## Thesis statement (to be completed by deep-dive)

TOB / tender-offer target. Typical playbook: long target into spread; size to annualized IRR. If the filer (buyer side) is the ticker, re-check direction during deep-dive — trading companies (8001 Itochu) sometimes file as the *buyer*, in which case thesis is different.

## Steelman of the opposite view

**Pending deep-dive.** Likely angles:
- Market already priced in the disclosure (no edge).
- Terms / magnitude less favorable than headline implies.
- Counter-catalyst exists (regulatory, activist, competing bid).
- Translation / interpretation error — revisit tc if direction flips on re-read.

## Web research layer (mandatory — pending)

- Nikkei / Reuters / Bloomberg coverage of Aeon Co., Ltd.
- Peer precedents for the same signal_type in JP market
- Shareholder register (activist presence?)
- Analyst coverage & recent target price moves

## Kill conditions

- **Kill 1:** Subsequent TDnet filing revising terms unfavorably — flip or cut.
- **Kill 2:** Board or independent committee opposes (for takeover signals); regulatory action escalates (for litigation signals).
- **Kill 3:** Translation re-read flips thesis direction → exit.
- **Kill 4:** Scanner flags superseding signal at the next cycle.

## Catalyst map (skeleton)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Primary filing | 2026-04-15 | (observed) | — |
| Board / regulatory response | T+5 to T+15 | Confirm support | Cut on opposition |
| Resolution window | varies | Maintain | Close near target |

## Position sizing

Satellite (2–5%). Adjust for liquidity of 8267.T; $30388M mcap implies institutional liquidity.

## Source traceability

- [tender_offer] https://www.release.tdnet.info/inbs/140120260415504769.pdf — signal_id 340f9fef654c51e2ec31ddb83d6e8b61
- [impairment_loss] https://www.release.tdnet.info/inbs/140120260409500830.pdf — signal_id b789ac16eb8d344248d72d1538cbcd40
- OpenFIGI: ticker=8267 mic=XTKS → figi=BBG000BN0FT1, issuer=BBG000BN0FD8
- Market cap: yfinance 8267.T → $30388M USD
- Convergence: D-004 hard-merge on (issuer_figi, signal_type, source_date)
