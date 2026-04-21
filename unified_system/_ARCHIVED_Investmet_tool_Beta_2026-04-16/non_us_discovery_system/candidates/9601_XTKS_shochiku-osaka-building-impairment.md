---
ticker_local: "9601"
mic: XTKS
ticker_plus_mic: 9601.XTKS
isin: null
figi: BBG000BNFMK9
issuer_figi: BBG000BNFM74
company_name_local: 松竹
company_name_en: "Shochiku Co., Ltd."
market_cap_usd_mm: 912
exchange: TDnet
country: JP
score: 31.0
convergence_bonus: 0
score_total: 31.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: 0.88
first_signal_date: 2026-04-14
last_updated: 2026-04-14
primary_catalyst_date: 2026-04-14
cross_listed_on: []
related_signal_ids: []
signal_type: impairment_loss
signal_category: results
scanner: tdnet
---

# Shochiku Co., Ltd. (9601.XTKS) — Osaka Shochikuza Building Demolition Impairment

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-14 Shochiku announced the commencement of demolition work on the Osaka Shochikuza Building, triggering a special loss (特別損失) booking. Scanner classifies this as an impairment_loss, strength_estimate=4, thesis_direction=short, with translation_confidence 0.88 (direction is disambiguated by the word 特別損失.計上 in the Japanese title). Score 31 puts this in immediate-route bucket; next step is a human deep-dive to determine magnitude vs. guidance and whether this is an isolated charge or signals broader real-estate-portfolio repositioning.

## Source signal

- **Signal** — https://www.release.tdnet.info/inbs/140120260414503559.pdf — 2026-04-14 JST — TDnet — Japanese-language impairment notice; [verified] title matches `特別損失.*計上` pattern; [inferred] Osaka Shochikuza is one of Shochiku's flagship Kansai venues (kabuki theater + shopping complex), so demolition implies rebuild or redevelopment.

## Translation notes

Japanese source. Machine-pattern-matched title only (translation_confidence 0.88 because 特別損失.計上 definitively signals a negative accounting event). Deep-dive must read the PDF body and translate key figures:
- 特別損失計上額 (amount of special loss)
- 計上時期 (fiscal quarter)
- 今期業績予想への影響 (impact on current-year forecast)
- 計上理由 (reason — is this rebuild-related, lease termination, asset write-off?)

## Company context (to be completed by deep-dive)

- Market cap: $912M USD (≈¥145B at 158.8 JPY/USD)
- Sector: Services — film/entertainment + real-estate operator. Long tail of owned theater real estate in Tokyo / Osaka.
- Trading status: N (normal) per scanner.
- 30/90-day price: TODO.
- Recent news: TODO — check for prior redevelopment announcements for Osaka Shochikuza site.
- Holders: TODO.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged `short` based on impairment-loss pattern. Deep-dive must:
- Size the loss vs. Shochiku's TTM operating profit
- Determine whether this is already embedded in current guidance or is incremental
- Check for a redevelopment PR pattern: if Shochiku is partnering with a real-estate developer, the short may be limited-duration and could flip long on the rebuild announcement
- Compare to prior Shochiku special-loss events (Kabukiza rebuild cycle?) for precedent

## Steelman of the opposite view

**Pending deep-dive.** Possible angles:
- Market has already priced in the demolition (redevelopment plan leaked earlier)
- Impairment is purely accounting (non-cash), Shochiku's real-estate NAV actually increases via rebuild
- Osaka venue is loss-making pre-demolition; booking a one-time charge to clear the books is a positive
- Shochiku's film library and IP (kabuki, Godzilla-era classics) carry value far beyond real estate

## Web research layer (mandatory — pending)

- Nikkei / Reuters coverage of Shochiku Osaka redevelopment plans pre-2026-04-14
- Analyst reaction: Nomura, Daiwa, SMBC Nikko coverage of Shochiku
- Comparable impairments at Shochiku historically (Kabukiza rebuild 2010-2013)
- Osaka Shochikuza Wikipedia / IR page for site history and seating capacity
- Any Shochiku FY25 annual report commentary on real-estate strategy

## Kill conditions

- **Kill 1:** Shochiku announces a concurrent redevelopment plan with partner and pre-leasing. Flip thesis.
- **Kill 2:** Loss amount < 1% of market cap and already in prior guidance. Discard — immaterial.
- **Kill 3:** Unexpected insurance recovery. Observable in follow-up TDnet filing.
- **(to refine with specific JPY thresholds during deep-dive)**

## Catalyst map (skeleton)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Tanshin (quarterly results) | Q1 FY26 release | N/A | - |
| Redevelopment partner announcement | Unknown | - | - |
| Guidance revision | T+0 to T+30 | - | - |

## Position sizing

Satellite (2-4%). Japanese small/mid-cap liquidity requires ADV check before sizing. Short-tag caveats: borrow availability on Shochiku (TOPIX member, generally borrowable).

## Source traceability

- https://www.release.tdnet.info/inbs/140120260414503559.pdf — retrieved 2026-04-14 via tdnet scanner
- OpenFIGI: ticker=9601 mic=XTKS → figi=BBG000BNFMK9, issuer_figi=BBG000BNFM74 (cached 2026-04-14)
- Market cap: yfinance 9601.T, JPY ¥144.9B × USD/JPY 0.006298 → $912M USD (2026-04-14)
- Japanese title: 大阪松竹座ビル解体工事の着手に伴う特別損失の計上に関するお知らせ
  (literal: "Notice regarding the booking of a special loss in connection with commencement of demolition work on the Osaka Shochikuza Building")
