---
ticker_local: "6027"
mic: XTKS
ticker_plus_mic: 6027.XTKS
isin: null
figi: BBG007HP08F6
issuer_figi: BBG007HP08C9
company_name_local: 弁護士ドットコム
company_name_en: "Bengo4.com,Inc."
market_cap_usd_mm: 389
exchange: TDnet
country: JP
score: 31.0
convergence_bonus: 0
score_total: 31.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: 0.88
first_signal_date: 2026-04-15
last_updated: 2026-04-15
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["c0a18d72aca8e690a6ada7a6ead291fe"]
signal_type: impairment_loss
signal_category: results
scanner: tdnet
---

# Bengo4.com,Inc. (6027.XTKS) — Impairment Loss

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

TDnet filed 2026-04-15 for Bengo4.com,Inc. (6027). Scanner classified signal_type=impairment_loss at score_total=31.0, translation_confidence=0.88, thesis_direction=short. Market cap $389M USD. 1 related filing(s) merged via D-004 convergence.

## Source signals

- [impairment_loss] https://www.release.tdnet.info/inbs/140120260415504708.pdf — signal_id c0a18d72aca8e690a6ada7a6ead291fe

## Translation notes

Japanese source. Translation pattern is unambiguous at tc=0.88. Deep-dive must still read the PDF for specifics (price, premium, timing, counterparty).

## Company context

- Market cap: $389M USD
- Sector / sub-sector: TODO (deep-dive)
- Recent price action: TODO (30/90d)
- Cross-listings: TODO (check HK / ADR / LSE)

## Thesis statement (to be completed by deep-dive)

Impairment or special loss disclosure. Direction=short on weakness in affected segment; check whether the market was surprised (vs. pre-announced).

## Steelman of the opposite view

**Pending deep-dive.** Likely angles:
- Market already priced in the disclosure (no edge).
- Terms / magnitude less favorable than headline implies.
- Counter-catalyst exists (regulatory, activist, competing bid).
- Translation / interpretation error — revisit tc if direction flips on re-read.

## Web research layer (mandatory — pending)

- Nikkei / Reuters / Bloomberg coverage of Bengo4.com,Inc.
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

Satellite (2–5%). Adjust for liquidity of 6027.T; $389M mcap implies moderate liquidity — verify ADV before sizing.

## Source traceability

- [impairment_loss] https://www.release.tdnet.info/inbs/140120260415504708.pdf — signal_id c0a18d72aca8e690a6ada7a6ead291fe
- OpenFIGI: ticker=6027 mic=XTKS → figi=BBG007HP08F6, issuer=BBG007HP08C9
- Market cap: yfinance 6027.T → $389M USD
- Convergence: D-004 hard-merge on (issuer_figi, signal_type, source_date)
