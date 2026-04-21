---
ticker_local: "WBC"
mic: XASX
ticker_plus_mic: WBC.XASX
isin: AU000000WBC1
figi: BBG000D0JD87
issuer_figi: BBG000D0JD23
company_name_local: "WESTPAC BANKING CORPORATION"
company_name_en: "Westpac Banking Corporation"
market_cap_usd_mm: 101170
exchange: ASX
country: AU
score: 30.0
convergence_bonus: 0
score_total: 30.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: "n/a"
first_signal_date: 2026-04-13
last_updated: 2026-04-14
primary_catalyst_date: 2026-04-13
cross_listed_on: []
related_signal_ids: []
signal_type: results_items_impacting
signal_category: results
scanner: asx
---

# Westpac Banking Corporation (WBC.XASX) — "Items Impacting Half Year 2026 Results" pre-announcement

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-13 (22:01 UTC) Westpac released a price-sensitive ASX announcement titled "Items Impacting Half Year 2026 Results" under the PERIODIC REPORTS category (documentKey `2924-03078284-2A1666269`). In Australian bank practice, a standalone "items impacting" release ahead of a half-year result is near-universally a pre-announcement of one-off charges — typically restructuring, software/intangibles impairment, customer-remediation provisions, or legal/regulatory settlements — that management wants to disclose before the formal 1H26 result. Scanner classifies as `results_items_impacting`, strength=5, thesis_direction=short, price-sensitive flag set. Score 30 puts this in the immediate-route bucket. The deep-dive must determine (a) the magnitude of the charge, (b) whether it is cash vs. non-cash, (c) whether consensus has already embedded it, and (d) whether it signals a repeat pattern across the Big Four.

## Source signal

- **Signal** — ASX announcement `2924-03078284-2A1666269` — 2026-04-13 22:01:01 UTC — ASX/markitdigital — headline "Items Impacting Half Year 2026 Results"; isPriceSensitive=true; PDF 522KB; [inferred] matches the well-known "Items Impacting Results" convention Westpac used in 2018, 2019, 2020, and 2024 to pre-announce one-off charges before the half-year release.
- The markitdigital API does not expose the PDF URL; operator will need to pull the PDF directly from the ASX Market Announcements page at https://www.asx.com.au/markets/company/WBC once the deep-dive starts.

## Translation notes

English source, translation_confidence = n/a. No interpretive risk on the scanner side.

## Company context (to be completed by deep-dive)

- Market cap: $101.2B USD (AUD ~$142B at 0.7138). One of Australia's "Big Four" banks.
- Sector: Banks (GICS).
- Listing: ASX primary; also trades on NZX under WBC.NZ (secondary). Not ADR-listed in the US.
- Half-year reporting cadence: Westpac's fiscal year ends 30 September. 1H runs Oct–Mar, result usually released early May. An April pre-announcement is consistent with that cadence — this is roughly 2–3 weeks before the scheduled result.
- Trading status: N (normal) — no trading halt observed as of the scan.
- 30/90-day price: TODO. Pull from yfinance `WBC.AX` close history.
- Recent news: TODO — check Reuters, AFR, SMH for any foreshadowing (analyst notes, APRA actions, ASIC proceedings) in the 30 days before 2026-04-13.
- Holders: TODO. WBC is heavily held by Australian super funds (AustralianSuper, Aware, AusSuper); major offshore holders typically include Vanguard, BlackRock.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged `short` based on pre-announcement pattern.

Deep-dive must:
- Read the PDF to extract:
  - Total pre-tax charge (AUD)
  - Cash vs. non-cash split
  - Specific line-items (restructuring, customer remediation, software impairment, hedge ineffectiveness, notable items)
  - Whether management is reaffirming or revising the prior guidance for cash earnings / ROE / CET1
  - Any change to the dividend outlook
- Size the charge vs.:
  - Consensus 1H26 cash earnings (typically ~$3.5B AUD range pre-items — confirm)
  - Westpac's CET1 ratio (any <10 bp impact vs. >30 bp impact changes the story materially)
  - Historical "items impacting" magnitudes: 2020 notable items were ~$1.2B; 2018 AUSTRAC penalty was ~$1.3B
- Cross-check whether the other Big Four (CBA, NAB, ANZ) have made similar pre-announcements in the same window — if so, this is a sector-wide regulatory/accounting story (e.g. APRA-driven provisioning change) and the short is less differentiated.
- Decide on instrument:
  - **Straight short WBC** — cleanest expression, but Big Four shorts are heavily regulated/squeezed
  - **Pair short WBC vs. long CBA** — if the charge is WBC-specific (idiosyncratic)
  - **Buy put spreads on WBC into the 1H26 result** — if options are liquid enough (ASX-listed WBC options are reasonably liquid out to 3-month tenor)

## Steelman of the opposite view

- **Kitchen-sink / new-CEO reset**: If the charge coincides with a CEO transition, the market often treats pre-announcements as clearing the decks and rewards the stock on the ex-items number. Check if Anthony Miller (CEO since Dec 2024) is using this as his first major half to reset the baseline — if yes, *long* is plausible.
- **Already-priced**: "Items impacting" is a known Westpac convention and analysts often model some provision buffer; a small charge (<$200M AUD) may be a non-event or even a relief rally if it's lower than whisper numbers.
- **Non-cash software impairment**: If the charge is entirely non-cash (e.g. writing down a failed tech platform), CET1 and dividend capacity are unaffected; short may work for 24–48 hours then fade.
- **Peer read-through positive**: If the charge is specific to WBC (e.g. a one-off legal settlement) and not a systemic provisioning change, CBA/NAB/ANZ can outperform — this could compress the WBC short into a *relative* trade rather than absolute.
- **Buyback/capital-management offset**: Westpac has been running a ~$1B buyback. Management sometimes pairs "items impacting" with a capital-management reaffirmation, neutralizing the signal. PDF read required.

## Web research layer (mandatory — pending)

- AFR / The Australian / Reuters coverage of the 2026-04-13 announcement (next trading day = 2026-04-14 AEST)
- Macquarie, MS, UBS, Jefferies, Citi Westpac 1H26 preview notes from April 2026
- APRA's Q1 2026 ADI Performance Statistics — look for sector-wide provisioning trends
- ASIC enforceable undertakings against WBC in 2025–2026
- Comparison to WBC's 2024 1H "items impacting" release (if present in signal_log) — magnitude and stock reaction
- Peer pre-announcements by CBA (Sep-Dec cycle), NAB (Mar/Sep cycle), ANZ (Mar/Sep cycle) in April 2026
- Westpac options chain on ASX: Apr/May/Jun expiries, ATM IV and skew

## Kill conditions

- **Kill 1:** Total pre-tax charge < $300M AUD AND cash-earnings guidance reaffirmed AND dividend outlook unchanged. Signal is immaterial; discard.
- **Kill 2:** The charge is fully non-cash AND Westpac simultaneously announces an incremental buyback or on-market capital return. Asymmetry flips; close short.
- **Kill 3:** Peer Big Four banks make similar pre-announcements in the same 5-day window. Signal is a sector provisioning reset, not idiosyncratic — covered by existing bank-sector exposure; no alpha.
- **Kill 4:** WBC share price gaps down >5% on the open 2026-04-14 AEST. Short entry no longer attractive; re-evaluate as a post-reaction reversal candidate, not a pre-result short.
- **Kill 5:** PDF reveals a one-off *positive* item (e.g. insurance recovery, hedge gain). Flip thesis to long and size via put-writing.

## Catalyst map

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Pre-announcement ("items impacting") | 2026-04-13 (done) | — | — |
| Next-day trading reaction (ASX) | 2026-04-14 AEST | Open ≤ flat → short | Gap >5% → abort |
| Broker note cascade | 2026-04-14 / 2026-04-15 | Multi-broker downgrade → add | Multi-broker upgrade → trim |
| 1H26 Result release | Early May 2026 (expected) | — | Deliver vs. post-items cons → exit |
| 1H26 dividend declaration | With result | — | Reaffirmed at prior run-rate → cover short |
| Investor briefing / call Q&A | With result | — | Management walk-forward of "items" magnitude → cover |

## Position sizing

Satellite (2–4%). Big Four banks are heavily shorted by offshore funds; borrow is generally available but can be expensive into earnings. Confirm short-rebate rate and HIN-level borrow availability before sizing. ADV on WBC is typically ~$250M AUD/day, so liquidity is not a constraint at sub-$100M portfolio sizes. Options expression (put spread) is preferred if 1M-3M IV is <22% — cheaper than carrying short borrow.

## Source traceability

- ASX announcement `2924-03078284-2A1666269` retrieved 2026-04-14 via `tools/asx_scanner.py` from `asx.api.markitdigital.com/asx-research/1.0/companies/WBC/announcements`
- OpenFIGI: ticker=WBC mic=XASX → figi=BBG000D0JD87, issuer_figi=BBG000D0JD23 (cached 2026-04-14)
- Market cap: $101.17B USD — from `working/asx_universe.json` (as_of 2026-04-14T16:08:27Z; yfinance WBC.AX Ticker.info × AUDUSD=X 0.7138)
- Scanner pattern matched: `\bitems impacting\b` (in `ASX_TITLE_RULES` → signal_type=`results_items_impacting`)
- ASX headline: "Items Impacting Half Year 2026 Results"
- Announcement type (ASX): "PERIODIC REPORTS"
- File: 522 KB PDF (not fetched — URL not exposed by markitdigital API; operator must pull from ASX Market Announcements page at time of deep-dive)
