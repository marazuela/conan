---
ticker_local: "1882"
mic: XTKS
ticker_plus_mic: 1882.XTKS
isin: null
figi: BBG000BKP0Y7
issuer_figi: BBG000BKP0P7
company_name_local: 東亜道
company_name_en: "Toa Road Corporation"
market_cap_usd_mm: 485
exchange: TDnet
country: JP
score: 29.0
convergence_bonus: 0
score_total: 29.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: 0.85
first_signal_date: 2026-04-15
last_updated: 2026-04-15
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["f35c58bdeaaa243c1afb4e30f151aa59"]
signal_type: litigation_regulatory
signal_category: governance
scanner: tdnet
---

# Toa Road Corporation (1882.XTKS) — Litigation Regulatory

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

TDnet filed 2026-04-15 for Toa Road Corporation (1882). Scanner classified signal_type=litigation_regulatory at score_total=29.0, translation_confidence=0.85, thesis_direction=short. Market cap $485M USD. 1 related filing(s) merged via D-004 convergence.

## Source signals

- [litigation_regulatory] https://www.release.tdnet.info/inbs/140120260413502990.pdf — signal_id f35c58bdeaaa243c1afb4e30f151aa59

## Translation notes

Japanese source. Translation pattern is unambiguous at tc=0.85. Deep-dive must still read the PDF for specifics (price, premium, timing, counterparty).

## Company context

- Market cap: $485M USD
- Sector / sub-sector: TODO (deep-dive)
- Recent price action: TODO (30/90d)
- Cross-listings: TODO (check HK / ADR / LSE)

## Thesis statement (to be completed by deep-dive)

Litigation or regulatory action. Direction=short on headline-risk overhang; verify materiality relative to mcap before sizing.

## Steelman of the opposite view

**Pending deep-dive.** Likely angles:
- Market already priced in the disclosure (no edge).
- Terms / magnitude less favorable than headline implies.
- Counter-catalyst exists (regulatory, activist, competing bid).
- Translation / interpretation error — revisit tc if direction flips on re-read.

## Web research layer (mandatory — pending)

- Nikkei / Reuters / Bloomberg coverage of Toa Road Corporation
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

Satellite (2–5%). Adjust for liquidity of 1882.T; $485M mcap implies moderate liquidity — verify ADV before sizing.

## Source traceability

- [litigation_regulatory] https://www.release.tdnet.info/inbs/140120260413502990.pdf — signal_id f35c58bdeaaa243c1afb4e30f151aa59
- OpenFIGI: ticker=1882 mic=XTKS → figi=BBG000BKP0Y7, issuer=BBG000BKP0P7
- Market cap: yfinance 1882.T → $485M USD
- Convergence: D-004 hard-merge on (issuer_figi, signal_type, source_date)
