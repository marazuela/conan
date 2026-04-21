---
ticker_local: "4343"
mic: XTKS
ticker_plus_mic: 4343.XTKS
isin: null
figi: BBG000LTC703
issuer_figi: BBG000LTB5H0
company_name_local: "イオンファンタジー"
company_name_en: "AEON Fantasy Co., Ltd."
market_cap_usd_mm: 311
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

# AEON Fantasy (4343.XTKS) — Non-operating income + special losses booking

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-09 (04:30 UTC / 13:30 JST) AEON Fantasy — the indoor family entertainment / kids-amusement subsidiary of the AEON group, operating "Mollyfantasy" and "Kidzooona" arcades inside AEON malls — released a TDnet filing titled 「営業外収益及び特別損失の計上に関するお知らせ」 ("Notice of booking of non-operating income and special losses"). Scanner classifies signal_strength=4, thesis_direction=short, tc=0.88 (above the 0.85 direction-allow floor). Score 31 — immediate-route. The dual booking (non-op income AND special losses in the same release) is unusual; the most common pattern is an insurance/tax refund recovery on the income side paired with store-closure/impairment charges on the loss side. Market cap $311M USD is just above the floor — low liquidity.

## Source signal

- **Source URL**: https://www.release.tdnet.info/inbs/140120260409500600.pdf
- **Filing date/time**: 2026-04-09 04:30 UTC (13:30 JST)
- **Local-language headline**: 「営業外収益及び特別損失の計上に関するお知らせ」
- **Signal ID**: `6b4296297b9890745bbe6fb2f7252f10`

## Translation notes

Translation confidence 0.88 — above the 0.85 direction-allow floor but below high-confidence. Direction tag `short` is pattern-matched on `特別損失` ("special losses"). Deep-dive MUST read the PDF to confirm:
- Actual yen magnitude of the special loss
- Cause (store closure? impairment of overseas subsidiaries, e.g. China/ASEAN? non-recurring restructuring?)
- Size of the offsetting non-operating income (insurance recovery? gain on sale? foreign-exchange?)
- Net P&L impact vs. prior guidance

## Company context (to be completed by deep-dive)

- Market cap: $311M USD (JPY ~¥46B at 150 JPY/USD). At the $300M floor — liquidity-constrained for institutional sizing.
- Sector: Consumer Services / Leisure (amusement/entertainment).
- Parent: AEON Group (8267 itself saw a separate TDnet tender-offer signal this cycle — cross-reference).
- Business: indoor kids-amusement arcades operated inside AEON malls in Japan + overseas (China, ASEAN). Overseas expansion is the known impairment-risk area.
- Fiscal year: AEON Fantasy's FY ends February, so this April 2026 filing likely relates to FY2026 (Feb 2026) results preview OR an FY2027 (Feb 2027) Q1 pre-announcement.
- 30/90-day price: TODO. Pull from yfinance `4343.T`.
- Recent news: TODO — check Nikkei, Bloomberg JP for overseas store-closure rumors, China same-store-sales warnings, or wage-cost pressure stories in Q1 2026.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged `short` — standard TDnet 特別損失 pattern.

Deep-dive must:
- Read the PDF to extract:
  - Special loss amount (JPY), and line-item (impairment, restructuring, litigation, inventory write-down)
  - Non-operating income amount and source
  - Net impact on FY guidance (has management revised the forecast?)
  - Whether a concurrent 業績予想の修正 ("guidance revision") filing was made
- Check for companion filings on the same day (4343 TDnet index for 2026-04-09)
- Size the loss vs. consensus FY2026 net income (Nikkei/Bloomberg JP consensus)
- Consider instruments:
  - **Straight short 4343** — direct but low liquidity (avg daily volume thin)
  - **Pair short 4343 vs. long 8267 (AEON parent)** — if the loss is 4343-idiosyncratic, the parent may be insulated
  - **Avoid options** — Japan single-name options on mid-caps at this size are illiquid

## Steelman of the opposite view

A "non-op income + special loss" dual booking can be **net-neutral or net-positive** for cash earnings if the non-operating income is a large insurance recovery, tax refund, or gain-on-sale of a non-core asset, and the special loss is a one-time overseas impairment that management had already telegraphed. In that scenario:
- The market has priced in the impairment already (30-day drift would show this)
- The offsetting income is fresh news
- The stock can rally on the release because cash EPS beats the prior-guidance midpoint
- A reflexive short on `特別損失` headline gets run over

Before sizing any short, deep-dive must confirm the special loss is NET LARGER than the offsetting income, that guidance is being cut (not held), and that the market had not telegraphed the impairment already.

## Web research layer (deep-dive TODO)

- Search Nikkei Asia for "AEON Fantasy China" / "Mollyfantasy" closures in last 90 days
- Check IR calendar at https://www.fantasy.co.jp/ir/ for FY2026 results date
- Look for analyst notes from Nomura, Daiwa, SMBC Nikko — recent rating changes or target cuts
- Cross-reference AEON group (8267) same-week filings — parent-level color on the subsidiary

## Kill conditions

- **Kill if** deep-dive PDF shows net non-op income > special loss (reverses direction)
- **Kill if** guidance is HELD or RAISED alongside the notice
- **Kill if** the special loss is < ¥500M (immaterial vs. ¥46B market cap → ~1% hit)
- **Kill if** stock is already down >10% in the 30 days pre-filing (priced in)
- **Kill if** liquidity check (20-day ADV) < $1M USD-equivalent — un-tradeable

## Catalyst map

- **Primary catalyst**: FY2026 results release (typically April for a February-end FY) — watch for concurrent guidance revision
- **Window**: 0–30 days
- **Entry trigger**: deep-dive confirms loss > income AND guidance cut
- **Exit trigger**: post-results reversal, earnings-day gap up, or hit kill condition

## Position sizing

Placeholder — do not size until deep-dive complete. Liquidity floor: 4343 20-day ADV must be confirmed. Target portfolio weight: 0.5–1.0% net short on conviction, half-size on tc=0.88 confidence.

## Source traceability

- Signal hash: `604d9e0bbcb71782d10f83f2`
- TDnet PDF: https://www.release.tdnet.info/inbs/140120260409500600.pdf
- Scanner: `tdnet` / Phase 2
- Scan date: 2026-04-15
