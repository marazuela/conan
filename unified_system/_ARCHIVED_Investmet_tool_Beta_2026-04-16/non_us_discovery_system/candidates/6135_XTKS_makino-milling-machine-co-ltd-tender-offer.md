---
ticker_local: "6135"
mic: XTKS
ticker_plus_mic: 6135.XTKS
isin: null
figi: BBG000BLH591
issuer_figi: BBG000BLH4Q5
company_name_local: 牧野フライス
company_name_en: "Makino Milling Machine Co., Ltd."
market_cap_usd_mm: 1710
exchange: TDnet
country: JP
score: 35.0
convergence_bonus: 0
score_total: 35.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: 0.92
first_signal_date: 2026-04-10
last_updated: 2026-04-15
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["4c473f015f2caf3a242d2818e2d91b91"]
signal_type: tender_offer
signal_category: takeover
scanner: tdnet
---

# Makino Milling Machine Co., Ltd. (6135.XTKS) — Tender Offer

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

TDnet filed 2026-04-10 for Makino Milling Machine Co., Ltd. (6135). Scanner classified signal_type=tender_offer at score_total=35.0, translation_confidence=0.92, thesis_direction=long. Market cap $1710M USD. 1 related filing(s) merged via D-004 convergence.

## Source signals

- [tender_offer] https://www.release.tdnet.info/inbs/140120260410501710.pdf — signal_id 4c473f015f2caf3a242d2818e2d91b91

## Translation notes

Japanese source. Translation pattern is unambiguous at tc=0.92. Deep-dive must still read the PDF for specifics (price, premium, timing, counterparty).

## Company context

- Market cap: $1710M USD
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

- Nikkei / Reuters / Bloomberg coverage of Makino Milling Machine Co., Ltd.
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
| Primary filing | 2026-04-10 | (observed) | — |
| Board / regulatory response | T+5 to T+15 | Confirm support | Cut on opposition |
| Resolution window | varies | Maintain | Close near target |

## Position sizing

Satellite (2–5%). Adjust for liquidity of 6135.T; $1710M mcap implies moderate liquidity — verify ADV before sizing.

## Source traceability

- [tender_offer] https://www.release.tdnet.info/inbs/140120260410501710.pdf — signal_id 4c473f015f2caf3a242d2818e2d91b91
- OpenFIGI: ticker=6135 mic=XTKS → figi=BBG000BLH591, issuer=BBG000BLH4Q5
- Market cap: yfinance 6135.T → $1710M USD
- Convergence: D-004 hard-merge on (issuer_figi, signal_type, source_date)
