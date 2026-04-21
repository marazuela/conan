---
ticker_local: "3391"
mic: XTKS
ticker_plus_mic: 3391.XTKS
isin: null
figi: BBG000JXTFN6
issuer_figi: BBG000JXTF38
company_name_local: ツルハＨＤ
company_name_en: "Tsuruha Holdings Inc."
market_cap_usd_mm: 5812
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
related_signal_ids: ["24e7b05c5e5600ae400f8d3109e51b96", "5d8a1dc37016276a5ea4e38590994984"]
signal_type: tender_offer
signal_category: takeover
scanner: tdnet
---

# Tsuruha Holdings Inc. (3391.XTKS) — Tender Offer

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

TDnet filed 2026-04-15 for Tsuruha Holdings Inc. (3391). Scanner classified signal_type=tender_offer at score_total=35.0, translation_confidence=0.92, thesis_direction=long. Market cap $5812M USD. 2 related filing(s) merged via D-004 convergence.

## Source signals

- [tender_offer] https://www.release.tdnet.info/inbs/140120260415504841.pdf — signal_id 24e7b05c5e5600ae400f8d3109e51b96
- [impairment_loss] https://www.release.tdnet.info/inbs/140120260408500386.pdf — signal_id 5d8a1dc37016276a5ea4e38590994984

## Translation notes

Japanese source. Translation pattern is unambiguous at tc=0.92. Deep-dive must still read the PDF for specifics (price, premium, timing, counterparty).

## Company context

- Market cap: $5812M USD
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

- Nikkei / Reuters / Bloomberg coverage of Tsuruha Holdings Inc.
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

Satellite (2–5%). Adjust for liquidity of 3391.T; $5812M mcap implies institutional liquidity.

## Source traceability

- [tender_offer] https://www.release.tdnet.info/inbs/140120260415504841.pdf — signal_id 24e7b05c5e5600ae400f8d3109e51b96
- [impairment_loss] https://www.release.tdnet.info/inbs/140120260408500386.pdf — signal_id 5d8a1dc37016276a5ea4e38590994984
- OpenFIGI: ticker=3391 mic=XTKS → figi=BBG000JXTFN6, issuer=BBG000JXTF38
- Market cap: yfinance 3391.T → $5812M USD
- Convergence: D-004 hard-merge on (issuer_figi, signal_type, source_date)
